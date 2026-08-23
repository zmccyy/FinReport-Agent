package com.finreport.mq;

import java.util.LinkedHashMap;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.stereotype.Component;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.finreport.domain.dto.ChatDtos.ChatStreamRequest;

/**
 * 问答消息生产者 — spec §3.3 控制流（chat.exchange）。
 *
 * <p>问答链路的数据面走 L2 → L3 HTTP SSE 拉流，控制面（会话请求的
 * 持久化记录）走 MQ：消息投递到 {@code q.chat.requests} 供 L3 消费
 * 记账/审计。发布是尽力而为——发布失败只记 WARN，不影响前端 SSE 流
 * （数据面独立于控制面，spec §3.3 混合模式）。</p>
 *
 * <p>消息体含 {@code idempotencyKey = messageId}，L3 消费者据此幂等；
 * traceId 经 header 透传。</p>
 */
@Component
public class ChatMessageProducer {

    private static final Logger log = LoggerFactory.getLogger(ChatMessageProducer.class);

    static final String CHAT_EXCHANGE = "chat.exchange";
    static final String ROUTING_KEY = "chat";

    private final RabbitTemplate rabbitTemplate;
    private final ObjectMapper objectMapper;

    public ChatMessageProducer(RabbitTemplate rabbitTemplate) {
        this.rabbitTemplate = rabbitTemplate;
        this.objectMapper = new ObjectMapper();
    }

    /**
     * 发布一条问答控制流消息（尽力而为，失败不抛出）。
     *
     * @param request L3 流式请求体（sessionId/messageId/reportId/question 等）
     * @param traceId 链路 trace ID
     */
    public void publishChat(ChatStreamRequest request, String traceId) {
        try {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("sessionId", request.sessionId());
            body.put("messageId", request.messageId());
            body.put("reportId", request.reportId());
            body.put("question", request.question());
            body.put("companyContext", request.companyContext());
            body.put("timestamp", java.time.Instant.now().toString());

            org.springframework.amqp.core.MessageProperties properties = new MessageProperties();
            properties.setContentType(MessageProperties.CONTENT_TYPE_JSON);
            properties.setDeliveryMode(MessageDeliveryMode.PERSISTENT); // spec §8.3 durable
            properties.setHeader("traceId", traceId);
            properties.setHeader("taskId", request.sessionId());
            properties.setHeader("idempotencyKey", request.messageId());

            rabbitTemplate.send(
                    CHAT_EXCHANGE,
                    ROUTING_KEY,
                    new org.springframework.amqp.core.Message(
                            objectMapper.writeValueAsBytes(body), properties));
            log.debug("[ChatMessageProducer] 已发布问答控制流消息 messageId={}", request.messageId());
        } catch (Exception error) {
            // 控制面失败不阻断数据面（spec §3.3：两者解耦）。
            log.warn("[ChatMessageProducer] 问答控制流消息发布失败 messageId={} error={}",
                    request.messageId(), error.getMessage());
        }
    }
}
