package com.finreport.mq;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.nio.charset.StandardCharsets;
import java.util.Map;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.http.codec.ServerSentEvent;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.finreport.domain.entity.Task;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.service.orchestrator.TaskOrchestrator;
import com.finreport.service.sse.RedisSseEventStore;
import com.finreport.service.sse.SseEmitterPool;
import com.rabbitmq.client.Channel;

import reactor.core.publisher.Mono;

/**
 * ProgressConsumer 单元测试。
 */
@ExtendWith(MockitoExtension.class)
@DisplayName("ProgressConsumer")
class ProgressConsumerTest {

    @Mock
    private SseEmitterPool ssePool;

    @Mock
    private TaskOrchestrator orchestrator;

    @Mock
    private Channel channel;

    @Mock
    private RedisSseEventStore eventStore;

    private ProgressConsumer consumer;
    private ObjectMapper objectMapper;

    @BeforeEach
    void setUp() {
        consumer = new ProgressConsumer(ssePool, orchestrator);
        objectMapper = new ObjectMapper();
    }

    @Nested
    @DisplayName("onProgress")
    class OnProgress {

        @Test
        @DisplayName("should ack and route to SSE on valid success message")
        void shouldAckAndRouteOnValidMessage() throws Exception {
            String json = objectMapper.writeValueAsString(Map.of(
                    "taskId", "task-abc", "step", "PARSE", "status", "SUCCESS",
                    "progress", 25, "result", Map.of("pages", 100)));

            MessageProperties props = new MessageProperties();
            Message message = new Message(json.getBytes(StandardCharsets.UTF_8), props);

            Task mockTask = new Task();
            mockTask.setId("task-abc");
            mockTask.setStatus(TaskStatus.PARSE_SUCCESS.name());

            when(orchestrator.handleStepProgress(eq("task-abc"), eq("PARSE"),
                    eq("SUCCESS"), any())).thenReturn(Mono.just(mockTask));

            consumer.onProgress(message, channel, 1L);

            // SSE progress event emitted
            verify(ssePool, times(1)).emit(eq("task-abc"), any());
            // Task is not terminal (PARSE_SUCCESS), so complete() not called
            verify(ssePool, never()).complete(anyString());
            verify(channel, times(1)).basicAck(1L, false);
        }

        @Test
        @DisplayName("should ack and skip SSE when message is malformed")
        void shouldNackMalformedMessageToDeadLetter() throws Exception {
            String json = objectMapper.writeValueAsString(Map.of("someField", "someValue"));
            MessageProperties props = new MessageProperties();
            Message message = new Message(json.getBytes(StandardCharsets.UTF_8), props);

            consumer.onProgress(message, channel, 2L);

            verify(channel, times(1)).basicNack(2L, false, false);
            verify(ssePool, never()).emit(anyString(), any());
        }

        @Test
        @DisplayName("should nack on JSON parse failure")
        void shouldNackOnParseFailure() throws Exception {
            MessageProperties props = new MessageProperties();
            Message message = new Message("invalid json".getBytes(StandardCharsets.UTF_8), props);

            consumer.onProgress(message, channel, 3L);

            verify(channel, times(1)).basicNack(3L, false, false);
        }

        @Test
        @DisplayName("should complete sink when task becomes terminal")
        void shouldCompleteSinkOnTerminal() throws Exception {
            String json = objectMapper.writeValueAsString(Map.of(
                    "taskId", "task-done", "step", "REPORT", "status", "SUCCESS",
                    "progress", 100));

            MessageProperties props = new MessageProperties();
            Message message = new Message(json.getBytes(StandardCharsets.UTF_8), props);

            Task terminalTask = new Task();
            terminalTask.setId("task-done");
            terminalTask.setStatus(TaskStatus.COMPLETED.name());
            terminalTask.setRefReportId(100L);

            when(orchestrator.handleStepProgress(eq("task-done"), eq("REPORT"),
                    eq("SUCCESS"), any())).thenReturn(Mono.just(terminalTask));

            consumer.onProgress(message, channel, 4L);

            // Progress event + done event = 2 emits
            verify(ssePool, times(2)).emit(eq("task-done"), any());
            verify(ssePool, times(1)).complete("task-done");
            verify(channel, times(1)).basicAck(4L, false);
        }

        @Test
        @DisplayName("should ack terminal message when local SSE completion throws")
        void shouldAckTerminalMessageWhenLocalSseCompletionThrows() throws Exception {
            String json = objectMapper.writeValueAsString(Map.of(
                    "taskId", "task-complete-failure", "step", "REPORT", "status", "SUCCESS",
                    "progress", 100));
            Message message = new Message(json.getBytes(StandardCharsets.UTF_8), new MessageProperties());

            Task terminalTask = new Task();
            terminalTask.setId("task-complete-failure");
            terminalTask.setStatus(TaskStatus.COMPLETED.name());
            terminalTask.setRefReportId(101L);
            when(orchestrator.handleStepProgress(eq("task-complete-failure"), eq("REPORT"),
                    eq("SUCCESS"), any())).thenReturn(Mono.just(terminalTask));
            doThrow(new IllegalStateException("local emitter cleanup failed"))
                    .when(ssePool).complete("task-complete-failure");

            consumer.onProgress(message, channel, 41L);

            verify(channel).basicAck(41L, false);
            verify(channel, never()).basicNack(41L, false, false);
        }

        @Test
        @DisplayName("should restore traceId from message header")
        void shouldRestoreTraceId() throws Exception {
            String json = objectMapper.writeValueAsString(Map.of(
                    "taskId", "task-trace", "step", "PARSE", "status", "RUNNING",
                    "progress", 10));

            MessageProperties props = new MessageProperties();
            props.setHeader("traceId", "trace-12345");
            Message message = new Message(json.getBytes(StandardCharsets.UTF_8), props);

            Task mockTask = new Task();
            mockTask.setId("task-trace");
            mockTask.setStatus(TaskStatus.PARSE_RUNNING.name());

            when(orchestrator.handleStepProgress(anyString(), anyString(), anyString(), any()))
                    .thenReturn(Mono.just(mockTask));

            consumer.onProgress(message, channel, 5L);

            verify(channel, times(1)).basicAck(5L, false);
        }

        @Test
        @DisplayName("should nack when durable SSE persistence returns no event")
        void shouldNackWhenDurableSsePersistenceReturnsEmpty() throws Exception {
            consumer = new ProgressConsumer(ssePool, orchestrator, eventStore);
            String json = objectMapper.writeValueAsString(Map.of(
                    "taskId", "task-empty-event", "step", "PARSE", "status", "RUNNING",
                    "progress", 10));
            Message message = new Message(json.getBytes(StandardCharsets.UTF_8), new MessageProperties());
            Task task = new Task();
            task.setId("task-empty-event");
            task.setStatus(TaskStatus.PARSE_RUNNING.name());
            when(orchestrator.handleStepProgress(anyString(), anyString(), anyString(), any()))
                    .thenReturn(Mono.just(task));
            when(eventStore.append(eq("task-empty-event"), any())).thenReturn(Mono.empty());

            consumer.onProgress(message, channel, 7L);

            verify(channel).basicNack(7L, false, false);
            verify(channel, never()).basicAck(7L, false);
            verify(ssePool, never()).emit(anyString(), any());
        }

        @Test
        @DisplayName("should ack when durable SSE persisted but local broadcast throws")
        void shouldAckWhenDurableSsePersistedButLocalBroadcastThrows() throws Exception {
            consumer = new ProgressConsumer(ssePool, orchestrator, eventStore);
            String json = objectMapper.writeValueAsString(Map.of(
                    "taskId", "task-local-sse-error", "step", "PARSE", "status", "RUNNING",
                    "progress", 10));
            Message message = new Message(json.getBytes(StandardCharsets.UTF_8), new MessageProperties());
            Task task = new Task();
            task.setId("task-local-sse-error");
            task.setStatus(TaskStatus.PARSE_RUNNING.name());
            ServerSentEvent<String> persisted = ServerSentEvent.<String>builder()
                    .event("progress").id("task-local-sse-error:1").data("{}").build();
            when(orchestrator.handleStepProgress(anyString(), anyString(), anyString(), any()))
                    .thenReturn(Mono.just(task));
            when(eventStore.append(eq("task-local-sse-error"), any())).thenReturn(Mono.just(persisted));
            doThrow(new IllegalStateException("local fan-out unavailable"))
                    .when(ssePool).emit(eq("task-local-sse-error"), any());

            consumer.onProgress(message, channel, 8L);

            verify(channel).basicAck(8L, false);
            verify(channel, never()).basicNack(8L, false, false);
        }

        @Test
        @DisplayName("should nack when orchestrator returns no persisted task")
        void shouldNackWhenOrchestratorReturnsNoPersistedTask() throws Exception {
            String json = objectMapper.writeValueAsString(Map.of(
                    "taskId", "task-no-result", "step", "PARSE", "status", "RUNNING"));
            Message message = new Message(json.getBytes(StandardCharsets.UTF_8), new MessageProperties());
            when(orchestrator.handleStepProgress(anyString(), anyString(), anyString(), any()))
                    .thenReturn(Mono.empty());

            consumer.onProgress(message, channel, 9L);

            verify(channel).basicNack(9L, false, false);
            verify(channel, never()).basicAck(9L, false);
        }

        @Test
        @DisplayName("should nack when persisted task status is invalid")
        void shouldNackWhenPersistedTaskStatusIsInvalid() throws Exception {
            String json = objectMapper.writeValueAsString(Map.of(
                    "taskId", "task-invalid-status", "step", "PARSE", "status", "RUNNING"));
            Message message = new Message(json.getBytes(StandardCharsets.UTF_8), new MessageProperties());
            Task task = new Task();
            task.setId("task-invalid-status");
            task.setStatus("NOT_A_TASK_STATUS");
            when(orchestrator.handleStepProgress(anyString(), anyString(), anyString(), any()))
                    .thenReturn(Mono.just(task));

            consumer.onProgress(message, channel, 10L);

            verify(channel).basicNack(10L, false, false);
            verify(channel, never()).basicAck(10L, false);
        }

        @Test
        @DisplayName("should nack to DLQ when orchestrator throws")
        void shouldNackToDeadLetterWhenOrchestratorThrows() throws Exception {
            String json = objectMapper.writeValueAsString(Map.of(
                    "taskId", "task-err", "step", "PARSE", "status", "SUCCESS",
                    "progress", 15));

            MessageProperties props = new MessageProperties();
            Message message = new Message(json.getBytes(StandardCharsets.UTF_8), props);

            when(orchestrator.handleStepProgress(anyString(), anyString(), anyString(), any()))
                    .thenThrow(new RuntimeException("DB error"));

            consumer.onProgress(message, channel, 6L);

            verify(ssePool, never()).emit(eq("task-err"), any());
            verify(channel, times(1)).basicNack(6L, false, false);
        }
    }
}
