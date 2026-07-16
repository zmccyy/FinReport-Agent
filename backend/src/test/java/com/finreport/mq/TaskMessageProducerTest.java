package com.finreport.mq;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;

import java.util.Map;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.rabbit.core.RabbitTemplate;

/**
 * {@link TaskMessageProducer} 单元测试。
 */
@ExtendWith(MockitoExtension.class)
@DisplayName("TaskMessageProducer")
class TaskMessageProducerTest {

    @Mock
    private RabbitTemplate rabbitTemplate;

    @Test
    @DisplayName("should preserve explicit trace ID in RabbitMQ headers")
    void shouldPreserveExplicitTraceIdInRabbitMqHeaders() {
        TaskMessageProducer producer = new TaskMessageProducer(rabbitTemplate);
        ArgumentCaptor<Message> messageCaptor = ArgumentCaptor.forClass(Message.class);

        producer.publishTaskStep("task-1", "parse", Map.of("pdfObjectKey", "reports/a.pdf"),
                "trace-from-http");

        verify(rabbitTemplate).convertAndSend(
                eq(TaskMessageProducer.TASK_EXCHANGE), eq("parse"), messageCaptor.capture());
        assertEquals("trace-from-http",
                messageCaptor.getValue().getMessageProperties().getHeader("traceId"));
        assertEquals("task-1:parse",
                messageCaptor.getValue().getMessageProperties().getHeader("idempotencyKey"));
    }
}
