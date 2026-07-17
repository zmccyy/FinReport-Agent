package com.finreport.mq;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.amqp.core.Message;
import org.springframework.beans.factory.annotation.Autowired;
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
import com.finreport.service.sse.RedisSseEventStore;
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
    private final RedisSseEventStore eventStore;
    private final ObjectMapper objectMapper;

    /** Backward-compatible constructor for legacy isolated unit tests. */
    public ProgressConsumer(SseEmitterPool ssePool, TaskOrchestrator orchestrator) {
        this(ssePool, orchestrator, null);
    }

    /** Creates the consumer with a durable SSE event store. */
    @Autowired
    public ProgressConsumer(
            SseEmitterPool ssePool, TaskOrchestrator orchestrator, RedisSseEventStore eventStore) {
        this.ssePool = ssePool;
        this.orchestrator = orchestrator;
        this.eventStore = eventStore;
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
                // 格式错误属于不可恢复的业务失败，必须进入 DLQ 供排查。
                channel.basicNack(deliveryTag, false, false);
                return;
            }

            // ---- 1. 先持久化编排状态；失败必须抛出并进入 DLQ ----
            Task updatedTask = orchestrator.handleStepProgress(taskId, step, status, result)
                    .block(ORCHESTRATOR_TIMEOUT);
            if (updatedTask == null) {
                throw new IllegalStateException("任务编排器未返回更新后的任务: " + taskId);
            }

            // ---- 2. Redis persist succeeds before local SSE fan-out. Persist failures are
            // intentionally fatal so the original progress message is inspected in the DLQ.
            pushToSse(taskId, step, status, progress, result);

            // ---- 3. 任务终态 → persist terminal event + close SSE sink ----
            TaskStatus taskStatus = TaskStatus.valueOf(updatedTask.getStatus());
            if (taskStatus.isTerminal()) {
                if (taskStatus == TaskStatus.COMPLETED) {
                    pushDoneEvent(taskId, updatedTask.getRefReportId());
                } else if (taskStatus == TaskStatus.FAILED) {
                    pushErrorEvent(taskId, step, "TASK_FAILED",
                            updatedTask.getErrorMsg() != null
                                    ? updatedTask.getErrorMsg() : "任务失败");
                }
                completeLocalSse(taskId, taskStatus);
            }

            // ---- 4. 仅在所有关键业务操作完成后 ACK ----
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
     * Persists and publishes a progress event.
     */
    private void pushToSse(String taskId, String step, String status, int progress,
            Map<String, Object> result) throws IOException {
        Map<String, Object> eventData = new java.util.LinkedHashMap<>();
        eventData.put("taskId", taskId);
        eventData.put("step", step);
        eventData.put("status", status);
        eventData.put("progress", progress);
        if (!result.isEmpty()) {
            eventData.put("result", result);
        }
        persistThenBroadcast(taskId, ServerSentEvent.<String>builder()
                .event("progress")
                .data(objectMapper.writeValueAsString(eventData))
                .build());
    }

    /**
     * Closes local SSE subscribers after a durable terminal event was stored.
     *
     * <p>The local pool is only a best-effort fan-out optimization. A cleanup failure must not
     * turn an already persisted task transition into a broker retry or DLQ message.</p>
     *
     * @param taskId task identifier
     * @param taskStatus terminal task status
     */
    private void completeLocalSse(String taskId, TaskStatus taskStatus) {
        try {
            ssePool.complete(taskId);
            log.info("[ProgressConsumer] 任务终结，SSE sink 已关闭 taskId={} status={}",
                    taskId, taskStatus);
        } catch (Exception error) {
            log.warn("[ProgressConsumer] SSE 本地关闭异常 taskId={} status={}",
                    taskId, taskStatus, error);
        }
    }

    /**
     * Persists and publishes a task completion event.
     */
    private void pushDoneEvent(String taskId, Long reportId) throws IOException {
        Map<String, Object> doneData = new java.util.LinkedHashMap<>();
        doneData.put("taskId", taskId);
        doneData.put("reportId", reportId);
        persistThenBroadcast(taskId, ServerSentEvent.<String>builder()
                .event("done")
                .data(objectMapper.writeValueAsString(doneData))
                .build());
    }

    /**
     * Persists and publishes a terminal error event.
     */
    private void pushErrorEvent(String taskId, String step, String code, String message) throws IOException {
        Map<String, Object> errorData = new java.util.LinkedHashMap<>();
        errorData.put("taskId", taskId);
        errorData.put("step", step);
        errorData.put("code", code);
        errorData.put("message", message);
        persistThenBroadcast(taskId, ServerSentEvent.<String>builder()
                .event("error")
                .data(objectMapper.writeValueAsString(errorData))
                .build());
    }

    /**
     * Persists before local fan-out. A persistence failure is propagated to force NACK/DLQ;
     * a local fan-out failure is deliberately only logged because the durable history is intact.
     */
    private void persistThenBroadcast(String taskId, ServerSentEvent<String> event) {
        if (eventStore == null) {
            try {
                if (!ssePool.emit(taskId, event)) {
                    log.warn("[ProgressConsumer] SSE 本地广播失败 taskId={}", taskId);
                }
            } catch (Exception error) {
                log.warn("[ProgressConsumer] SSE 本地广播异常 taskId={}", taskId, error);
            }
            return;
        }
        ServerSentEvent<String> persisted = eventStore.append(taskId, event)
                .block(ORCHESTRATOR_TIMEOUT);
        if (persisted == null) {
            throw new IllegalStateException("SSE progress event was not persisted: " + taskId);
        }
        try {
            if (!ssePool.emit(taskId, persisted)) {
                log.warn("[ProgressConsumer] SSE 本地广播失败 taskId={}", taskId);
            }
        } catch (Exception error) {
            log.warn("[ProgressConsumer] SSE 本地广播异常 taskId={}", taskId, error);
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
