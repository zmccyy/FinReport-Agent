package com.finreport.controller;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
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
import com.finreport.service.sse.RedisSseEventStore;
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
    private final RedisSseEventStore eventStore;
    private final ObjectMapper objectMapper;

    /** Backward-compatible constructor for isolated controller tests. */
    public TaskController(TaskOrchestrator orchestrator, SseEmitterPool ssePool) {
        this(orchestrator, ssePool, null);
    }

    /** Creates a task controller with Redis as the SSE recovery source of truth. */
    @Autowired
    public TaskController(
            TaskOrchestrator orchestrator, SseEmitterPool ssePool, RedisSseEventStore eventStore) {
        this.orchestrator = orchestrator;
        this.ssePool = ssePool;
        this.eventStore = eventStore;
        this.objectMapper = new ObjectMapper();
    }

    /**
     * SSE 进度流 — spec §6.3.2。
     *
     * <p>{@code GET /api/v1/tasks/{id}/stream}
     * 返回 {@code text/event-stream}，持续推送 progress / done / error 事件。
     * 客户端断线后通过 {@code Last-Event-ID} 头重连。Redis 是恢复历史的唯一来源，
     * 当前进程的 {@code SseEmitterPool} 仅用于实时 fan-out。</p>
     *
     * <p>若任务已处于终态，直接返回一条 done/error 事件后关闭连接。</p>
     *
     * @param taskId      任务 ID
     * @param lastEventId 客户端断线重连时传递的最后事件 ID（格式：taskId:sequence）
     * @return SSE 事件流
     */
    @GetMapping(value = "/tasks/{id}/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<ServerSentEvent<String>> streamProgress(
            @PathVariable("id") String taskId,
            @RequestHeader("X-User-Id") Long userId,
            @RequestHeader(value = "Last-Event-ID", required = false) String lastEventId) {
        log.debug("[TaskController] SSE 订阅 taskId={} userId={} lastEventId={}",
                taskId, userId, lastEventId);
        return orchestrator.findByIdAndUserId(taskId, userId)
                .switchIfEmpty(Mono.error(new BusinessException(
                        HttpStatus.NOT_FOUND, "TASK_NOT_FOUND", "任务不存在: " + taskId)))
                .flatMapMany(task -> buildStream(taskId, lastEventId, task));
    }

    private Flux<ServerSentEvent<String>> buildStream(String taskId, String lastEventId, Task task) {
        Flux<ServerSentEvent<String>> realtime = ssePool.subscribeRealtime(taskId);
        Flux<ServerSentEvent<String>> recovered = eventStore == null
                ? Flux.empty()
                : eventStore.replay(taskId, lastEventId);
        if (parseStatus(task).isTerminal()) {
            return recovered.switchIfEmpty(Flux.from(buildTerminalEvent(taskId, parseStatus(task), task)));
        }
        Flux<ServerSentEvent<String>> events = Flux.merge(recovered, realtime)
                .distinct(ServerSentEvent::id);
        Flux<ServerSentEvent<String>> heartbeat = Flux.interval(HEARTBEAT_INTERVAL)
                .map(tick -> ServerSentEvent.<String>builder().comment("heartbeat").build());
        return Flux.merge(events, heartbeat)
                .doOnSubscribe(ignored -> log.info("[TaskController] SSE 连接建立 taskId={}", taskId))
                .doOnCancel(() -> log.info("[TaskController] SSE 连接断开 taskId={}", taskId));
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
    public Mono<ResponseEntity<Task>> getTask(
            @PathVariable("id") String taskId,
            @RequestHeader("X-User-Id") Long userId) {
        log.debug("[TaskController] GET /tasks/{} userId={}", taskId, userId);
        return orchestrator.findByIdAndUserId(taskId, userId)
                .map(ResponseEntity::ok)
                .switchIfEmpty(Mono.error(taskNotFound(taskId)));
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
    public Mono<ResponseEntity<Task>> cancelTask(
            @PathVariable("id") String taskId,
            @RequestHeader("X-User-Id") Long userId) {
        log.debug("[TaskController] POST /tasks/{}/cancel userId={}", taskId, userId);
        return orchestrator.cancelTask(taskId, userId)
                .flatMap(task -> publishCancelEvent(taskId, task).thenReturn(ResponseEntity.ok(task)))
                .switchIfEmpty(Mono.error(taskNotFound(taskId)));
    }

    /**
     * Persists the cancellation event before local fan-out. A durable SSE write failure is logged
     * but never rolls back an already committed cancellation.
     */
    private Mono<Void> publishCancelEvent(String taskId, Task task) {
        if (!TaskStatus.CANCELLED.name().equals(task.getStatus())) {
            return Mono.empty();
        }
        try {
            Map<String, Object> doneData = new LinkedHashMap<>();
            doneData.put("taskId", taskId);
            doneData.put("status", "CANCELLED");
            ServerSentEvent<String> event = ServerSentEvent.<String>builder()
                    .event("done")
                    .data(objectMapper.writeValueAsString(doneData))
                    .build();
            Mono<ServerSentEvent<String>> persisted = eventStore == null
                    ? Mono.just(event)
                    : eventStore.append(taskId, event);
            return persisted.doOnNext(stored -> {
                if (!ssePool.emit(taskId, stored)) {
                    log.warn("[TaskController] cancellation SSE local fan-out failed taskId={}", taskId);
                }
            })
                    .onErrorResume(error -> {
                        log.warn("[TaskController] cancellation SSE persistence failed taskId={}", taskId, error);
                        return Mono.empty();
                    })
                    .then(Mono.fromRunnable(() -> ssePool.complete(taskId)));
        } catch (JsonProcessingException error) {
            log.warn("[TaskController] cancellation event JSON serialization failed taskId={}", taskId, error);
            ssePool.complete(taskId);
            return Mono.empty();
        }
    }

    private static BusinessException taskNotFound(String taskId) {
        return new BusinessException(HttpStatus.NOT_FOUND, "TASK_NOT_FOUND", "任务不存在: " + taskId);
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
