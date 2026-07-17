package com.finreport.mq;

import java.time.Instant;
import java.util.Map;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.stereotype.Component;

import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * 任务消息生产者 — 向 task.exchange 发布原子步骤消息。
 *
 * <p>spec §3.1：所有消息 durable=true，delivery_mode=2，手动 ack。
 * 消息体含 idempotency_key=taskId+step（不含 retry 计数）。</p>
 */
@Component
public class TaskMessageProducer {

    private static final Logger log = LoggerFactory.getLogger(TaskMessageProducer.class);

    static final String TASK_EXCHANGE = "task.exchange";
    private static final String RETRY_EXCHANGE_PREFIX = "task.retry.";
    private static final int FIRST_RETRY = 1;
    private static final int SECOND_RETRY = 2;
    private static final int THIRD_RETRY = 3;

    private final RabbitTemplate rabbitTemplate;
    private final ObjectMapper objectMapper;

    public TaskMessageProducer(RabbitTemplate rabbitTemplate) {
        this.rabbitTemplate = rabbitTemplate;
        this.objectMapper = new ObjectMapper();
    }

    /**
     * 发布一个任务步骤消息。
     *
     * @param taskId  任务 ID
     * @param step    步骤名称（parse / extract.bs / extract.is / extract.cf / check / report）
     * @param payload 步骤参数（如 pdfObjectKey 等）
     */
    public void publishTaskStep(String taskId, String step, Map<String, Object> payload) {
        publishTaskStep(taskId, step, payload, MDC.get("traceId"));
    }

    /**
     * 发布一个任务步骤消息，并显式传递请求链路 trace ID。
     *
     * @param taskId 任务 ID
     * @param step 步骤名称
     * @param payload 步骤参数
     * @param traceId HTTP/Reactor 请求链路 trace ID
     */
    public void publishTaskStep(
            String taskId, String step, Map<String, Object> payload, String traceId) {
        publish(taskId, step, payload, traceId, 0, TASK_EXCHANGE);
    }

    /**
     * 发布延迟重试消息。消息仍保留同一 idempotencyKey；重试次数只写入消息 header。
     *
     * @param taskId 任务 ID
     * @param step 原始步骤 routing key
     * @param payload 步骤参数
     * @param retryCount 已安排的重试次数（1、2、3）
     * @param traceId 链路 trace ID
     */
    public void publishRetry(
            String taskId, String step, Map<String, Object> payload, int retryCount, String traceId) {
        String exchange = RETRY_EXCHANGE_PREFIX + retryDelayName(retryCount) + ".exchange";
        publish(taskId, step, payload, traceId, retryCount, exchange);
    }

    private void publish(
            String taskId,
            String step,
            Map<String, Object> payload,
            String traceId,
            int retryCount,
            String exchange) {
        String effectiveTraceId = traceId;
        if (effectiveTraceId == null || effectiveTraceId.isBlank()) {
            effectiveTraceId = UUID.randomUUID().toString();
        }
        try {
            Map<String, Object> messageBody = Map.of(
                    "taskId", taskId,
                    "step", step,
                    "payload", payload,
                    "timestamp", Instant.now().toString(),
                    "idempotencyKey", taskId + ":" + step);
            MessageProperties props = new MessageProperties();
            props.setContentType(MessageProperties.CONTENT_TYPE_JSON);
            props.setDeliveryMode(MessageDeliveryMode.PERSISTENT);
            props.setHeader("traceId", effectiveTraceId);
            props.setHeader("taskId", taskId);
            props.setHeader("step", step);
            props.setHeader("idempotencyKey", taskId + ":" + step);
            props.setHeader("x-retry-count", retryCount);
            rabbitTemplate.convertAndSend(exchange, step,
                    new Message(objectMapper.writeValueAsBytes(messageBody), props));
            log.info("[TaskMessageProducer] 消息已发布 exchange={} routingKey={} taskId={} step={} retry={}",
                    exchange, step, taskId, step, retryCount);
        } catch (Exception error) {
            log.error("[TaskMessageProducer] 消息发布失败 taskId={} step={}", taskId, step, error);
            throw new com.finreport.exception.IntegrationException(
                    org.springframework.http.HttpStatus.INTERNAL_SERVER_ERROR,
                    "MQ_PUBLISH_FAILED", "MQ 消息发布失败: " + step, error);
        }
    }

    private static String retryDelayName(int retryCount) {
        return switch (retryCount) {
            case FIRST_RETRY -> "1s";
            case SECOND_RETRY -> "5s";
            case THIRD_RETRY -> "30s";
            default -> throw new IllegalArgumentException("retryCount must be between 1 and 3");
        };
    }
}
