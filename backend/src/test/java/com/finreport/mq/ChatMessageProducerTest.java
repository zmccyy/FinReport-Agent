package com.finreport.mq;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;

import java.util.List;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.core.RabbitTemplate;

import com.finreport.domain.dto.ChatDtos.ChatStreamRequest;

/** ChatMessageProducer 单测：exchange/routing key/消息头。 */
@ExtendWith(MockitoExtension.class)
class ChatMessageProducerTest {

    @Mock
    private RabbitTemplate rabbitTemplate;

    @Test
    void shouldPublishToChatExchangeWithIdempotencyHeaders() throws Exception {
        ChatMessageProducer producer = new ChatMessageProducer(rabbitTemplate);
        ChatStreamRequest request = new ChatStreamRequest(
                "1", "m-uuid", 17L, "营收？", "贵州茅台（600519）", List.of(), null);

        producer.publishChat(request, "trace-1");

        ArgumentCaptor<Message> messageCaptor = ArgumentCaptor.forClass(Message.class);
        verify(rabbitTemplate).send(
                eq(ChatMessageProducer.CHAT_EXCHANGE),
                eq(ChatMessageProducer.ROUTING_KEY),
                messageCaptor.capture());

        Message sent = messageCaptor.getValue();
        MessageProperties props = sent.getMessageProperties();
        assertNotNull(props);
        assertEquals("trace-1", props.getHeader("traceId"));
        assertEquals("m-uuid", props.getHeader("idempotencyKey"));
        assertEquals("1", props.getHeader("taskId"));
        assertEquals(org.springframework.amqp.core.MessageDeliveryMode.PERSISTENT,
                props.getDeliveryMode(), "消息必须 durable（spec §8.3）");
        // 消息体 JSON 包含关键字段
        String body = new String(sent.getBody(), java.nio.charset.StandardCharsets.UTF_8);
        assertTrue(body.contains("\"messageId\":\"m-uuid\""));
        assertTrue(body.contains("\"question\":\"营收？\""));
    }

    @Test
    void shouldSwallowPublishFailures() {
        ChatMessageProducer producer = new ChatMessageProducer(rabbitTemplate);
        org.mockito.Mockito.doThrow(new RuntimeException("broker down"))
                .when(rabbitTemplate).send(any(String.class), any(String.class), any(Message.class));

        // 控制面失败不抛出（spec §3.3 解耦：数据面不受影响）
        producer.publishChat(
                new ChatStreamRequest("1", "m-1", 17L, "问题", "", List.of(), null), "trace-1");
    }
}
