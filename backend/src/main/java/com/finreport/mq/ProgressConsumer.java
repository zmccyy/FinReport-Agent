package com.finreport.mq;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.amqp.support.AmqpHeaders;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.messaging.handler.annotation.Header;
import org.springframework.stereotype.Component;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.finreport.domain.entity.Task;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.service.orchestrator.TaskOrchestrator;
import com.finreport.service.sse.SseEmitterPool;
import com.rabbitmq.client.Channel;

/**
 * 进度消息消费者 — spec §3.2 M1.10。
 *
 * <p>监听 {@code q.progress.results}（绑定 progress.exchange fanout），
 * 消费 L3 回报的步骤进度，按 taskId 路由到 SseEmitterPool 并更新任务状态。
 * 无活跃 SSE 订阅者时静默丢弃（不报错）。</p>
 *
 * <p>手动 ack：处理成功才 ack；异常时 nack(requeue=false) 进 DLQ。</p>
 */
@Component
public class ProgressConsumer {

    private static final Logger log = LoggerFactory.getLogger(ProgressConsumer.class);

    /**
     * {@link TaskOrchestrator#handleStepProgress} 阻塞等待超时。
     */
    private static final Duration ORCHESTRATOR_TIMEOUT = Duration.ofSeconds(5);

    private final SseEmitterPool ssePool;
    private final TaskOrchestrator orchestrator;
    private final ObjectMapper objectMapper;

    public ProgressConsumer(SseEmitterPool ssePool, TaskOrchestrator orchestrator) {
        this.ssePool = ssePool;
        this.orchestrator = orchestrator;
        this.objectMapper = new ObjectMapper();
    }

    /**
     * 消费 L3 进度消息并分发到 SSE 池和任务编排器。
     *
     * <p>队列 {@code q.progress.results} 绑定到 {@code progress.exchange} (fanout)。
     * 消息格式（JSON）：
     * <pre>{@code
     * {
     *   "taskId": "task-abc123",
     *   "step": "PARSE",
     *   "status": "SUCCESS",
     *   "progress": 25,
     *   "result": {...},
     *   "timestamp": "2026-07-14T20:30:00Z",
     *   "idempotencyKey": "task-abc123:PARSE"
     * }
     * }</pre>
     *
     * @param message 原始 AMQP 消息（绕过 JSON 转换器，等幂手动解析）
     * @param channel RabbitMQ Channel（用于手动 ack/nack）
     * @param deliveryTag 投递标签（由 Spring AMQP 从消息头提取）
     */
    @RabbitListener(queues = "q.progress.results", ackMode = "MANUAL")
    public void onProgress(Message message, Channel channel,
            @Header(AmqpHeaders.DELIVERY_TAG) long deliveryTag) {
        String taskId = null;
        String step = null;
        String status = null;

        try {
            // 从消息头恢复 traceId
            Object traceId = message.getMessageProperties().getHeader("traceId");
            if (traceId instanceof String tid && !tid.isEmpty()) {
                MDC.put("traceId", tid);
            }

            // 解析消息体
            String json = new String(message.getBody(), StandardCharsets.UTF_8);
            Map<String, Object> body = objectMapper.readValue(json,
                    new TypeReference<Map<String, Object>>() {});

            taskId = getString(body, "taskId");
            step = getString(body, "step");
            status = getString(body, "status");
            @SuppressWarnings("unchecked")
            Map<String, Object> result = body.get("result") instanceof Map
                    ? (Map<String, Object>) body.get("result") : Map.of();
            int progress = body.get("progress") instanceof Number n ? n.intValue() : 0;

            log.debug("[ProgressConsumer] 收到进度 taskId={} step={} status={} progress={}",
                    taskId, step, status, progress);

            if (taskId == null || step == null || status == null) {
                log.warn("[ProgressConsumer] 消息缺少必要字段 taskId={} step={} status={}",
                        taskId, step, status);
                // 格式错误，不进 DLQ，直接 ack 丢弃
                channel.basicAck(deliveryTag, false);
                return;
            }

            // ---- 1. 推送到 SSE 池（无订阅者时 sink 缓存或静默丢弃） ----
            pushToSse(taskId, step, status, progress, result);

            // ---- 2. 更新任务编排器状态（返回值即更新后的 Task） ----
            Task updatedTask = null;
            try {
                updatedTask = orchestrator.handleStepProgress(taskId, step, status, result)
                        .block(ORCHESTRATOR_TIMEOUT);
            } catch (Exception e) {
                log.error("[ProgressConsumer] 任务编排失败 taskId={} step={}", taskId, step, e);
            }

            // ---- 3. 任务终态 → 推送终端事件 + 关闭 SSE sink ----
            if (updatedTask != null) {
                try {
                    TaskStatus taskStatus = TaskStatus.valueOf(updatedTask.getStatus());
                    if (taskStatus.isTerminal()) {
                        if (taskStatus == TaskStatus.COMPLETED) {
                            pushDoneEvent(taskId, updatedTask.getRefReportId());
                        } else if (taskStatus == TaskStatus.FAILED) {
                            pushErrorEvent(taskId, step, "TASK_FAILED",
                                    updatedTask.getErrorMsg() != null
                                            ? updatedTask.getErrorMsg() : "任务失败");
                        }
                        // CANCELLED 不额外推送（cancel 端点已推送）
                        ssePool.complete(taskId);
                        log.info("[ProgressConsumer] 任务终结，SSE sink 已关闭 taskId={} status={}",
                                taskId, taskStatus);
                    }
                } catch (Exception e) {
                    log.debug("[ProgressConsumer] 终态检查跳过 taskId={} error={}",
                            taskId, e.getMessage());
                }
            }

            // ---- 4. 手动 ack ----
            channel.basicAck(deliveryTag, false);
            log.debug("[ProgressConsumer] 已 ack taskId={} step={}", taskId, step);

        } catch (Exception e) {
            log.error("[ProgressConsumer] 处理异常 taskId={} step={}", taskId, step, e);
            try {
                // nack 不进 DLQ，requeue=false（spec §3.1：失败进 DLQ）
                channel.basicNack(deliveryTag, false, false);
            } catch (IOException nackEx) {
                log.error("[ProgressConsumer] nack 失败 deliveryTag={}", deliveryTag, nackEx);
            }
        } finally {
            MDC.remove("traceId");
        }
    }

    // ========================================================================
    // Private helpers
    // ========================================================================

    /**
     * 将进度事件推送到 SSE 池。
     */
    private void pushToSse(String taskId, String step, String status, int progress,
            Map<String, Object> result) {
        try {
            Map<String, Object> eventData = new java.util.LinkedHashMap<>();
            eventData.put("taskId", taskId);
            eventData.put("step", step);
            eventData.put("status", status);
            eventData.put("progress", progress);
            if (!result.isEmpty()) {
                eventData.put("result", result);
            }

            String data = objectMapper.writeValueAsString(eventData);
            ServerSentEvent<String> sseEvent = ServerSentEvent.<String>builder()
                    .event("progress")
                    .data(data)
                    .id(taskId + ":" + step)
                    .build();

            ssePool.emit(taskId, sseEvent);
        } catch (Exception e) {
            log.warn("[ProgressConsumer] SSE 推送异常 taskId={}", taskId, e);
        }
    }

    /**
     * 推送任务完成事件到 SSE。
     */
    private void pushDoneEvent(String taskId, Long reportId) {
        try {
            Map<String, Object> doneData = new java.util.LinkedHashMap<>();
            doneData.put("taskId", taskId);
            doneData.put("reportId", reportId);

            String data = objectMapper.writeValueAsString(doneData);
            ServerSentEvent<String> sseEvent = ServerSentEvent.<String>builder()
                    .event("done")
                    .data(data)
                    .id(taskId + ":done")
                    .build();

            ssePool.emit(taskId, sseEvent);
        } catch (Exception e) {
            log.warn("[ProgressConsumer] done 事件推送异常 taskId={}", taskId, e);
        }
    }

    /**
     * 推送错误事件到 SSE。
     */
    private void pushErrorEvent(String taskId, String step, String code, String message) {
        try {
            Map<String, Object> errorData = new java.util.LinkedHashMap<>();
            errorData.put("taskId", taskId);
            errorData.put("step", step);
            errorData.put("code", code);
            errorData.put("message", message);

            String data = objectMapper.writeValueAsString(errorData);
            ServerSentEvent<String> sseEvent = ServerSentEvent.<String>builder()
                    .event("error")
                    .data(data)
                    .id(taskId + ":error")
                    .build();

            ssePool.emit(taskId, sseEvent);
        } catch (Exception e) {
            log.warn("[ProgressConsumer] error 事件推送异常 taskId={}", taskId, e);
        }
    }

    /**
     * 从 Map 安全提取字符串值。
     */
    private static String getString(Map<String, Object> map, String key) {
        Object value = map.get(key);
        return value instanceof String s ? s : null;
    }
}
