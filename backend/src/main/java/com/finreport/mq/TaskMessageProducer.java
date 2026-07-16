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
        String routingKey = step;
        if (traceId == null || traceId.isBlank()) {
            traceId = UUID.randomUUID().toString();
        }

        try {
            Map<String, Object> messageBody = Map.of(
                    "taskId", taskId,
                    "step", step,
                    "payload", payload,
                    "timestamp", Instant.now().toString(),
                    "idempotencyKey", taskId + ":" + step
            );

            byte[] body = objectMapper.writeValueAsBytes(messageBody);

            MessageProperties props = new MessageProperties();
            props.setContentType(MessageProperties.CONTENT_TYPE_JSON);
            props.setDeliveryMode(MessageDeliveryMode.PERSISTENT);
            props.setHeader("traceId", traceId);
            props.setHeader("taskId", taskId);
            props.setHeader("step", step);
            props.setHeader("idempotencyKey", taskId + ":" + step);

            Message message = new Message(body, props);

            rabbitTemplate.convertAndSend(TASK_EXCHANGE, routingKey, message);
            log.info("[TaskMessageProducer] 消息已发布 exchange={} routingKey={} taskId={} step={} traceId={}",
                    TASK_EXCHANGE, routingKey, taskId, step, traceId);
        } catch (Exception e) {
            log.error("[TaskMessageProducer] 消息发布失败 taskId={} step={}", taskId, step, e);
            throw new com.finreport.exception.IntegrationException(
                    org.springframework.http.HttpStatus.INTERNAL_SERVER_ERROR,
                    "MQ_PUBLISH_FAILED",
                    "MQ 消息发布失败: " + step, e);
        }
    }
}
