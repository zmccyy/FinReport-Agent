package com.finreport.controller;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.finreport.domain.entity.Task;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.exception.BusinessException;
import com.finreport.service.orchestrator.TaskOrchestrator;
import com.finreport.service.sse.SseEmitterPool;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 任务与 SSE 控制器 — spec §6.2.3。
 *
 * <p>提供任务查询、取消和 SSE 进度流端点。
 * SSE 端点返回 {@code text/event-stream}，支持 Last-Event-ID 重连。</p>
 */
@RestController
@RequestMapping("/api/v1")
public class TaskController {

    private static final Logger log = LoggerFactory.getLogger(TaskController.class);

    /**
     * SSE 心跳间隔（每 30 秒发一个注释行，防中间代理断连）。
     */
    private static final Duration HEARTBEAT_INTERVAL = Duration.ofSeconds(30);

    private final TaskOrchestrator orchestrator;
    private final SseEmitterPool ssePool;
    private final ObjectMapper objectMapper;

    public TaskController(TaskOrchestrator orchestrator, SseEmitterPool ssePool) {
        this.orchestrator = orchestrator;
        this.ssePool = ssePool;
        this.objectMapper = new ObjectMapper();
    }

    /**
     * SSE 进度流 — spec §6.3.2。
     *
     * <p>{@code GET /api/v1/tasks/{id}/stream}
     * 返回 {@code text/event-stream}，持续推送 progress / done / error 事件。
     * 客户端断线后通过 {@code Last-Event-ID} 头重连，server 从 Sinks.Many
     * 重放缓存（replay().limit(16)）恢复。</p>
     *
     * <p>若任务已处于终态，直接返回一条 done/error 事件后关闭连接。</p>
     *
     * @param taskId      任务 ID
     * @param lastEventId 客户端断线重连时传递的最后事件 ID（用于日志记录；
     *                    实际重放由 replay sink 自动完成）
     * @return SSE 事件流
     */
    @GetMapping(value = "/tasks/{id}/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<ServerSentEvent<String>> streamProgress(
            @PathVariable("id") String taskId,
            @RequestHeader(value = "Last-Event-ID", required = false) String lastEventId) {

        log.debug("[TaskController] SSE 订阅 taskId={} lastEventId={}", taskId, lastEventId);

        // 先检查任务是否存在
        return orchestrator.findById(taskId)
                .switchIfEmpty(Mono.error(new BusinessException(
                        HttpStatus.NOT_FOUND,
                        "TASK_NOT_FOUND", "任务不存在: " + taskId)))
                .flatMapMany(task -> {
                    TaskStatus taskStatus = parseStatus(task);
                    if (taskStatus.isTerminal()) {
                        log.debug("[TaskController] 任务已终态 taskId={} status={}",
                                taskId, taskStatus);
                        return Flux.from(buildTerminalEvent(taskId, taskStatus, task));
                    }

                    // 任务进行中 → 订阅 SSE 流
                    Flux<ServerSentEvent<String>> eventStream = ssePool.subscribe(taskId);

                    // 心跳：eventStream 完成时通过 takeUntilOther 自动停止
                    Flux<ServerSentEvent<String>> heartbeat = Flux.interval(HEARTBEAT_INTERVAL)
                            .map(tick -> ServerSentEvent.<String>builder()
                                    .comment("heartbeat")
                                    .build())
                            .takeUntilOther(eventStream.then().thenReturn(true));

                    return Flux.merge(eventStream, heartbeat)
                            .doOnSubscribe(s -> log.info(
                                    "[TaskController] SSE 连接建立 taskId={}", taskId))
                            .doOnCancel(() -> log.info(
                                    "[TaskController] SSE 连接断开 taskId={}", taskId))
                            .doOnComplete(() -> log.info(
                                    "[TaskController] SSE 流完成 taskId={}", taskId));
                });
    }

    /**
     * 查询任务详情。
     *
     * <p>{@code GET /api/v1/tasks/{id}}</p>
     *
     * @param taskId 任务 ID
     * @return 任务实体
     */
    @GetMapping("/tasks/{id}")
    public Mono<ResponseEntity<Task>> getTask(@PathVariable("id") String taskId) {
        log.debug("[TaskController] GET /tasks/{}", taskId);
        return orchestrator.findById(taskId)
                .map(ResponseEntity::ok)
                .switchIfEmpty(Mono.just(ResponseEntity.notFound().build()));
    }

    /**
     * 取消任务。
     *
     * <p>{@code POST /api/v1/tasks/{id}/cancel}</p>
     *
     * @param taskId 任务 ID
     * @return 更新后的任务实体（含 404 如果任务不存在）
     */
    @PostMapping("/tasks/{id}/cancel")
    public Mono<ResponseEntity<Task>> cancelTask(@PathVariable("id") String taskId) {
        log.debug("[TaskController] POST /tasks/{}/cancel", taskId);
        return orchestrator.cancelTask(taskId)
                .map(task -> {
                    // 仅当状态实际变为 CANCELLED 时才推送 SSE 事件
                    if (TaskStatus.CANCELLED.name().equals(task.getStatus())) {
                        try {
                            Map<String, Object> doneData = new LinkedHashMap<>();
                            doneData.put("taskId", taskId);
                            doneData.put("status", "CANCELLED");

                            ServerSentEvent<String> cancelEvent = ServerSentEvent.<String>builder()
                                    .event("done")
                                    .data(objectMapper.writeValueAsString(doneData))
                                    .id(taskId + ":cancelled")
                                    .build();
                            ssePool.emit(taskId, cancelEvent);
                            ssePool.complete(taskId);
                        } catch (JsonProcessingException e) {
                            log.warn("[TaskController] JSON 序列化失败 taskId={}", taskId, e);
                        }
                    }
                    return ResponseEntity.ok(task);
                })
                .switchIfEmpty(Mono.just(ResponseEntity.notFound().build()));
    }

    // ========================================================================
    // Private helpers
    // ========================================================================

    /**
     * 为已终态的任务构建一条总结事件。
     */
    private Mono<ServerSentEvent<String>> buildTerminalEvent(
            String taskId, TaskStatus status, Task task) {
        try {
            Map<String, Object> dataMap = new LinkedHashMap<>();
            dataMap.put("taskId", taskId);

            String event;
            if (status == TaskStatus.COMPLETED) {
                event = "done";
                dataMap.put("reportId", task.getRefReportId());
                dataMap.put("status", "COMPLETED");
            } else if (status == TaskStatus.CANCELLED) {
                event = "done";
                dataMap.put("status", "CANCELLED");
            } else {
                event = "error";
                dataMap.put("code", "TASK_FAILED");
                dataMap.put("message",
                        task.getErrorMsg() != null ? task.getErrorMsg() : "任务失败");
            }

            String data = objectMapper.writeValueAsString(dataMap);
            return Mono.just(ServerSentEvent.<String>builder()
                    .event(event)
                    .data(data)
                    .id(taskId + ":terminal")
                    .build());
        } catch (JsonProcessingException e) {
            log.error("[TaskController] 终端事件 JSON 序列化失败 taskId={}", taskId, e);
            return Mono.empty();
        }
    }

    private static TaskStatus parseStatus(Task task) {
        try {
            return TaskStatus.valueOf(task.getStatus());
        } catch (IllegalArgumentException e) {
            return TaskStatus.PENDING;
        }
    }
}
