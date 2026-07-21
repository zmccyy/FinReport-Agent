package com.finreport.service.orchestrator;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doNothing;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.Map;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import com.finreport.domain.entity.Task;
import com.finreport.domain.entity.TaskStep;
import com.finreport.domain.enums.StepStatus;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.domain.enums.TaskStepName;
import com.finreport.exception.BusinessException;
import com.finreport.exception.IntegrationException;
import com.finreport.mq.TaskMessageProducer;
import com.finreport.repository.TaskRepository;
import com.finreport.repository.TaskStepRepository;
import com.finreport.trace.TraceContext;

import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * ExtractDispatcher 单元测试 — M2.08。
 *
 * <p>覆盖：</p>
 * <ul>
 *   <li>dispatchAll：tracker.reset + 3 条 concatMap 顺序发布 + idempotency（跳过非 PENDING）</li>
 *   <li>dispatchAll：MQ 发布失败 → markDispatchFailed（task + step FAILED）+ 上抛 IntegrationException</li>
 *   <li>dispatchAll：step 记录缺失 → BusinessException</li>
 *   <li>dispatchSingle：clearFailure + markRunningAndPublish</li>
 *   <li>dispatchSingle：MQ 发布失败 → markDispatchFailed + 上抛</li>
 * </ul>
 */
@ExtendWith(MockitoExtension.class)
@DisplayName("ExtractDispatcher")
class ExtractDispatcherTest {

    @Mock
    private TaskRepository taskRepo;

    @Mock
    private TaskStepRepository stepRepo;

    @Mock
    private TaskMessageProducer messageProducer;

    @Mock
    private ExtractCompletionTracker tracker;

    private ExtractDispatcher dispatcher;

    @BeforeEach
    void setUp() {
        dispatcher = new ExtractDispatcher(taskRepo, stepRepo, messageProducer, tracker);
    }

    private Task task(String id) {
        return Task.builder()
                .id(id)
                .userId(5L)
                .taskType("REPORT_PARSE")
                .status(TaskStatus.EXTRACT_RUNNING.name())
                .payload("{\"pdfObjectKey\":\"uploads/test.pdf\"}")
                .build();
    }

    private TaskStep pendingStep(String taskId, TaskStepName name) {
        return TaskStep.builder()
                .id(1L)
                .taskId(taskId)
                .stepName(name.name())
                .status(StepStatus.PENDING.name())
                .build();
    }

    private IntegrationException mqError() {
        return new IntegrationException(
                org.springframework.http.HttpStatus.SERVICE_UNAVAILABLE,
                "MQ_PUBLISH_FAILED", "RabbitMQ is unavailable");
    }

    @Nested
    @DisplayName("dispatchAll")
    class DispatchAll {

        @Test
        @DisplayName("should reset tracker and publish 3 extract messages in order")
        void shouldResetTrackerAndPublishThreeExtractMessagesInOrder() {
            Task task = task("task-dispatch-all");
            Map<String, Object> payload = Map.of("pdfObjectKey", "uploads/test.pdf");
            String traceId = "trace-dispatch";

            when(tracker.reset(task.getId())).thenReturn(Mono.empty());
            TaskStep bs = pendingStep(task.getId(), TaskStepName.EXTRACT_BS);
            TaskStep is = pendingStep(task.getId(), TaskStepName.EXTRACT_IS);
            TaskStep cf = pendingStep(task.getId(), TaskStepName.EXTRACT_CF);
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_BS")).thenReturn(Mono.just(bs));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_IS")).thenReturn(Mono.just(is));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_CF")).thenReturn(Mono.just(cf));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));

            StepVerifier.create(dispatcher.dispatchAll(task, payload)
                            .contextWrite(ctx -> ctx.put(TraceContext.TRACE_ID, traceId)))
                    .verifyComplete();

            assertEquals(StepStatus.RUNNING.name(), bs.getStatus());
            assertEquals(StepStatus.RUNNING.name(), is.getStatus());
            assertEquals(StepStatus.RUNNING.name(), cf.getStatus());
            verify(tracker).reset(task.getId());
            verify(messageProducer).publishTaskStep(task.getId(), "extract.bs", payload, traceId);
            verify(messageProducer).publishTaskStep(task.getId(), "extract.is", payload, traceId);
            verify(messageProducer).publishTaskStep(task.getId(), "extract.cf", payload, traceId);
        }

        @Test
        @DisplayName("should skip non-PENDING steps to keep idempotency on replay")
        void shouldSkipNonPendingStepsToKeepIdempotencyOnReplay() {
            Task task = task("task-replay");
            Map<String, Object> payload = Map.of("pdfObjectKey", "uploads/test.pdf");

            when(tracker.reset(task.getId())).thenReturn(Mono.empty());
            TaskStep bs = pendingStep(task.getId(), TaskStepName.EXTRACT_BS);
            TaskStep is = TaskStep.builder()
                    .id(2L).taskId(task.getId()).stepName("EXTRACT_IS")
                    .status(StepStatus.RUNNING.name()).build();
            TaskStep cf = pendingStep(task.getId(), TaskStepName.EXTRACT_CF);
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_BS")).thenReturn(Mono.just(bs));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_IS")).thenReturn(Mono.just(is));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_CF")).thenReturn(Mono.just(cf));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));

            StepVerifier.create(dispatcher.dispatchAll(task, payload)
                            .contextWrite(ctx -> ctx.put(TraceContext.TRACE_ID, "replay-trace")))
                    .verifyComplete();

            // BS + CF dispatched; IS skipped (already RUNNING)
            verify(messageProducer).publishTaskStep(
                    eq(task.getId()), eq("extract.bs"), any(Map.class), anyString());
            verify(messageProducer, never()).publishTaskStep(
                    eq(task.getId()), eq("extract.is"), any(Map.class), anyString());
            verify(messageProducer).publishTaskStep(
                    eq(task.getId()), eq("extract.cf"), any(Map.class), anyString());
        }

        @Test
        @DisplayName("should mark task and step FAILED when MQ publish fails mid-dispatch")
        void shouldMarkTaskAndStepFailedWhenMqPublishFails() {
            Task task = task("task-mq-fail");
            Map<String, Object> payload = Map.of("pdfObjectKey", "uploads/test.pdf");
            IntegrationException error = mqError();

            when(tracker.reset(task.getId())).thenReturn(Mono.empty());
            TaskStep bs = pendingStep(task.getId(), TaskStepName.EXTRACT_BS);
            TaskStep is = pendingStep(task.getId(), TaskStepName.EXTRACT_IS);
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_BS")).thenReturn(Mono.just(bs));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_IS")).thenReturn(Mono.just(is));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(taskRepo.save(any(Task.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            // BS publish succeeds; IS publish fails.
            // 显式 stub BS publish 让 Mockito strict stubbing 满意：dispatchAll 用 concatMap 顺序发布，
            // 严格模式下未 stub 的调用会被识别为参数不匹配。这里把 IS 的失败 stub 与 BS 的成功 stub 配对。
            doNothing().when(messageProducer).publishTaskStep(
                    eq(task.getId()), eq("extract.bs"), any(Map.class), anyString());
            doThrow(error).when(messageProducer).publishTaskStep(
                    eq(task.getId()), eq("extract.is"), any(Map.class), anyString());

            StepVerifier.create(dispatcher.dispatchAll(task, payload)
                            .contextWrite(ctx -> ctx.put(TraceContext.TRACE_ID, "fail-trace")))
                    .expectErrorMatches(ex -> ex instanceof IntegrationException integrationException
                            && "MQ_PUBLISH_FAILED".equals(integrationException.getErrorCode()))
                    .verify();

            // task marked FAILED by markDispatchFailed
            assertEquals(TaskStatus.FAILED.name(), task.getStatus());
            // IS step marked FAILED; BS already RUNNING (saved)
            assertEquals(StepStatus.RUNNING.name(), bs.getStatus());
            assertEquals(StepStatus.FAILED.name(), is.getStatus());
            // CF never queried (concatMap stops on error)
            verify(stepRepo, never()).findByTaskIdAndStepName(task.getId(), "EXTRACT_CF");
        }

        @Test
        @DisplayName("should throw BusinessException when step record missing")
        void shouldThrowBusinessExceptionWhenStepRecordMissing() {
            Task task = task("task-missing-step");
            Map<String, Object> payload = Map.of("pdfObjectKey", "uploads/test.pdf");

            when(tracker.reset(task.getId())).thenReturn(Mono.empty());
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_BS")).thenReturn(Mono.empty());

            StepVerifier.create(dispatcher.dispatchAll(task, payload))
                    .expectErrorMatches(ex -> ex instanceof BusinessException businessException
                            && "TASK_STEP_NOT_FOUND".equals(businessException.getErrorCode()))
                    .verify();
        }
    }

    @Nested
    @DisplayName("dispatchSingle")
    class DispatchSingle {

        @Test
        @DisplayName("should clear failure flag and publish single retry message")
        void shouldClearFailureFlagAndPublishSingleRetryMessage() {
            Task task = task("task-retry-single");
            Map<String, Object> payload = Map.of("pdfObjectKey", "uploads/test.pdf");
            String traceId = "retry-trace";

            when(tracker.clearFailure(task.getId())).thenReturn(Mono.empty());
            TaskStep is = pendingStep(task.getId(), TaskStepName.EXTRACT_IS);
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_IS")).thenReturn(Mono.just(is));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));

            StepVerifier.create(dispatcher.dispatchSingle(task, TaskStepName.EXTRACT_IS, payload)
                            .contextWrite(ctx -> ctx.put(TraceContext.TRACE_ID, traceId)))
                    .verifyComplete();

            verify(tracker).clearFailure(task.getId());
            assertEquals(StepStatus.RUNNING.name(), is.getStatus());
            verify(messageProducer).publishTaskStep(task.getId(), "extract.is", payload, traceId);
        }

        @Test
        @DisplayName("should mark task FAILED when single retry publish fails")
        void shouldMarkTaskFailedWhenSingleRetryPublishFails() {
            Task task = task("task-retry-fail");
            Map<String, Object> payload = Map.of("pdfObjectKey", "uploads/test.pdf");
            IntegrationException error = mqError();

            when(tracker.clearFailure(task.getId())).thenReturn(Mono.empty());
            TaskStep cf = pendingStep(task.getId(), TaskStepName.EXTRACT_CF);
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_CF")).thenReturn(Mono.just(cf));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(taskRepo.save(any(Task.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            doThrow(error).when(messageProducer).publishTaskStep(
                    eq(task.getId()), eq("extract.cf"), any(Map.class), anyString());

            StepVerifier.create(dispatcher.dispatchSingle(task, TaskStepName.EXTRACT_CF, payload))
                    .expectErrorMatches(ex -> ex instanceof IntegrationException integrationException
                            && "MQ_PUBLISH_FAILED".equals(integrationException.getErrorCode()))
                    .verify();

            assertEquals(TaskStatus.FAILED.name(), task.getStatus());
            assertEquals(StepStatus.FAILED.name(), cf.getStatus());
        }
    }
}
