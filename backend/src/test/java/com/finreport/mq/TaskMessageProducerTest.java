package com.finreport.mq;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.verify;

import java.util.Map;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageDeliveryMode;
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

    @Test
    @DisplayName("should publish persistent retry messages to every delay exchange")
    void shouldPublishPersistentRetryMessagesToEveryDelayExchange() {
        TaskMessageProducer producer = new TaskMessageProducer(rabbitTemplate);
        ArgumentCaptor<Message> messageCaptor = ArgumentCaptor.forClass(Message.class);

        producer.publishRetry("task-retry", "PARSE", Map.of("pdfObjectKey", "users/1/report.pdf"),
                1, "trace-retry");
        verify(rabbitTemplate).convertAndSend(eq("task.retry.1s.exchange"), eq("PARSE"),
                messageCaptor.capture());
        assertRetryMessage(messageCaptor.getValue(), 1, "trace-retry");

        producer.publishRetry("task-retry", "PARSE", Map.of(), 2, "trace-retry");
        verify(rabbitTemplate).convertAndSend(eq("task.retry.5s.exchange"), eq("PARSE"),
                messageCaptor.capture());
        assertRetryMessage(messageCaptor.getValue(), 2, "trace-retry");

        producer.publishRetry("task-retry", "PARSE", Map.of(), 3, "trace-retry");
        verify(rabbitTemplate).convertAndSend(eq("task.retry.30s.exchange"), eq("PARSE"),
                messageCaptor.capture());
        assertRetryMessage(messageCaptor.getValue(), 3, "trace-retry");
    }

    @Test
    @DisplayName("should generate trace ID when caller does not provide one")
    void shouldGenerateTraceIdWhenCallerDoesNotProvideOne() {
        TaskMessageProducer producer = new TaskMessageProducer(rabbitTemplate);
        ArgumentCaptor<Message> messageCaptor = ArgumentCaptor.forClass(Message.class);

        producer.publishTaskStep("task-new-trace", "PARSE", Map.of(), " ");

        verify(rabbitTemplate).convertAndSend(eq(TaskMessageProducer.TASK_EXCHANGE), eq("PARSE"),
                messageCaptor.capture());
        assertNotNull(messageCaptor.getValue().getMessageProperties().getHeader("traceId"));
        assertEquals(Integer.valueOf(0), messageCaptor.getValue().getMessageProperties().getHeader("x-retry-count"));
    }

    @Test
    @DisplayName("should reject retry count outside three configured buckets")
    void shouldRejectRetryCountOutsideThreeConfiguredBuckets() {
        TaskMessageProducer producer = new TaskMessageProducer(rabbitTemplate);

        assertThrows(IllegalArgumentException.class,
                () -> producer.publishRetry("task-invalid", "PARSE", Map.of(), 4, "trace"));
    }

    @Test
    @DisplayName("should wrap RabbitMQ publishing errors as integration exception")
    void shouldWrapRabbitMqPublishingErrorsAsIntegrationException() {
        TaskMessageProducer producer = new TaskMessageProducer(rabbitTemplate);
        doThrow(new IllegalStateException("broker unavailable")).when(rabbitTemplate)
                .convertAndSend(any(String.class), any(String.class), any(Message.class));

        assertThrows(com.finreport.exception.IntegrationException.class,
                () -> producer.publishTaskStep("task-error", "PARSE", Map.of(), "trace"));
    }

    private void assertRetryMessage(Message message, int retryCount, String traceId) {
        assertEquals(MessageDeliveryMode.PERSISTENT, message.getMessageProperties().getDeliveryMode());
        assertEquals("task-retry:PARSE", message.getMessageProperties().getHeader("idempotencyKey"));
        assertEquals(Integer.valueOf(retryCount), message.getMessageProperties().getHeader("x-retry-count"));
        assertEquals(traceId, message.getMessageProperties().getHeader("traceId"));
    }
}
