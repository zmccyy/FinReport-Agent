package com.finreport.service.orchestrator;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doReturn;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.Map;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.r2dbc.core.DatabaseClient;
import org.springframework.transaction.reactive.TransactionalOperator;

import com.finreport.domain.entity.Task;
import com.finreport.domain.entity.TaskStep;
import com.finreport.domain.enums.StepStatus;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.exception.IntegrationException;
import com.finreport.mq.TaskMessageProducer;
import com.finreport.repository.TaskRepository;
import com.finreport.repository.TaskStepRepository;
import com.finreport.trace.TraceContext;

import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * TaskOrchestrator 单元测试 — M1.09 骨架。
 */
@ExtendWith(MockitoExtension.class)
@DisplayName("TaskOrchestrator")
class TaskOrchestratorTest {

    @Mock
    private TaskRepository taskRepo;

    @Mock
    private TaskStepRepository stepRepo;

    @Mock
    private TaskMessageProducer messageProducer;

    @Mock
    private DatabaseClient databaseClient;

    @Mock
    private DatabaseClient.GenericExecuteSpec insertSpec;

    @Mock
    private TransactionalOperator transactionalOperator;

    private TaskStateMachine stateMachine;
    private TaskOrchestrator orchestrator;

    @BeforeEach
    void setUp() {
        stateMachine = new TaskStateMachine();
        orchestrator = new TaskOrchestrator(
                taskRepo, stepRepo, stateMachine, messageProducer, databaseClient, transactionalOperator);
    }

    @Nested
    @DisplayName("createTask")
    class CreateTask {

        @Test
        @DisplayName("should insert nullable task columns with bindNull")
        void shouldInsertNullableTaskColumnsWithBindNull() {
            stubTransactionalOperator();
            stubTaskInsert();
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(invocation ->
                    Mono.just(invocation.getArgument(0)));

            StepVerifier.create(orchestrator.createTaskWithoutDispatch(5L, null, Map.of()))
                    .assertNext(task -> assertEquals(5L, task.getUserId()))
                    .verifyComplete();

            verify(insertSpec).bindNull("refReportId", Long.class);
            verify(insertSpec).bindNull("currentStep", String.class);
        }

        @Test
        @DisplayName("should create task and dispatch PARSE step")
        void shouldCreateTaskAndDispatchParse() {
            stubTransactionalOperator();
            stubTaskInsert();
            // save task
            when(taskRepo.save(any(Task.class))).thenAnswer(inv -> {
                Task t = inv.getArgument(0);
                return Mono.just(t);
            });
            // save task_step
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(inv -> {
                TaskStep s = inv.getArgument(0);
                s.setId(1L);
                return Mono.just(s);
            });
            // dispatchStep → findByTaskIdAndStepName for PARSE
            when(stepRepo.findByTaskIdAndStepName(anyString(), eq("PARSE")))
                    .thenAnswer(inv -> {
                        TaskStep s = new TaskStep();
                        s.setId(1L);
                        s.setTaskId(inv.getArgument(0));
                        s.setStepName("PARSE");
                        s.setStatus(StepStatus.PENDING.name());
                        return Mono.just(s);
                    });
            // dispatchStep 需要重新查询 task
            when(taskRepo.findById(anyString())).thenAnswer(inv -> {
                Task t = new Task();
                t.setId(inv.getArgument(0));
                t.setUserId(5L);
                t.setTaskType("REPORT_PARSE");
                t.setStatus(TaskStatus.PENDING.name());
                return Mono.just(t);
            });

            Map<String, Object> payload = Map.of("pdfObjectKey", "reports/test.pdf");

            StepVerifier.create(orchestrator.createTask(5L, payload))
                    .assertNext(task -> {
                        assertNotNull(task.getId());
                        assertTrue(task.getId().startsWith("task-"));
                        assertEquals(5L, task.getUserId());
                        assertEquals("REPORT_PARSE", task.getTaskType());
                    })
                    .verifyComplete();
        }
    }

    private void stubTransactionalOperator() {
        doAnswer(invocation -> invocation.getArgument(0))
                .when(transactionalOperator).transactional(any(Mono.class));
    }

    private void stubTaskInsert() {
        when(databaseClient.sql(anyString())).thenReturn(insertSpec);
        doReturn(insertSpec).when(insertSpec).bind(anyString(), any());
        doReturn(insertSpec).when(insertSpec).bindNull(anyString(), any());
        when(insertSpec.then()).thenReturn(Mono.empty());
    }

    @Nested
    @DisplayName("dispatchTask")
    class DispatchTask {

        @Test
        @DisplayName("should propagate Reactor trace ID when dispatching MQ message")
        void shouldPropagateReactorTraceIdWhenDispatchingMessage() {
            Task task = Task.builder()
                    .id("task-trace")
                    .userId(5L)
                    .taskType("REPORT_PARSE")
                    .status(TaskStatus.PENDING.name())
                    .build();
            TaskStep step = TaskStep.builder()
                    .id(1L)
                    .taskId(task.getId())
                    .stepName("PARSE")
                    .status(StepStatus.PENDING.name())
                    .build();
            String traceId = "trace-from-http";

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "PARSE")).thenReturn(Mono.just(step));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));

            StepVerifier.create(orchestrator.dispatchTask(task.getId(), Map.of())
                            .contextWrite(context -> context.put(TraceContext.TRACE_ID, traceId)))
                    .expectNext(task)
                    .verifyComplete();

            verify(messageProducer).publishTaskStep(
                    eq(task.getId()), eq("parse"), any(Map.class), eq(traceId));
        }

        @Test
        @DisplayName("should mark task and step failed when MQ publishing fails")
        void shouldMarkTaskAndStepFailedWhenMqPublishingFails() {
            Task task = Task.builder()
                    .id("task-mq-failure")
                    .userId(5L)
                    .taskType("REPORT_PARSE")
                    .status(TaskStatus.PENDING.name())
                    .build();
            TaskStep step = TaskStep.builder()
                    .id(1L)
                    .taskId(task.getId())
                    .stepName("PARSE")
                    .status(StepStatus.PENDING.name())
                    .build();

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "PARSE")).thenReturn(Mono.just(step));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            doThrow(new IntegrationException(
                    org.springframework.http.HttpStatus.SERVICE_UNAVAILABLE,
                    "MQ_PUBLISH_FAILED", "RabbitMQ is unavailable"))
                    .when(messageProducer).publishTaskStep(
                            eq(task.getId()), eq("parse"), any(Map.class), anyString());

            StepVerifier.create(orchestrator.dispatchTask(task.getId(), Map.of("pdfObjectKey", "reports/test.pdf")))
                    .expectErrorMatches(error -> error instanceof IntegrationException
                            && "MQ_PUBLISH_FAILED".equals(((IntegrationException) error).getErrorCode()))
                    .verify();

            assertEquals(TaskStatus.FAILED.name(), task.getStatus());
            assertEquals(StepStatus.FAILED.name(), step.getStatus());
            verify(taskRepo, times(2)).save(task);
            verify(stepRepo, times(2)).save(step);
        }
    }

    @Nested
    @DisplayName("findById")
    class FindById {

        @Test
        @DisplayName("should return task when found")
        void shouldReturnTask() {
            Task task = new Task();
            task.setId("task-abc123");
            task.setStatus(TaskStatus.PENDING.name());
            when(taskRepo.findById("task-abc123")).thenReturn(Mono.just(task));

            StepVerifier.create(orchestrator.findById("task-abc123"))
                    .assertNext(found -> assertEquals("task-abc123", found.getId()))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return empty when not found")
        void shouldReturnEmpty() {
            when(taskRepo.findById("task-nonexist")).thenReturn(Mono.empty());

            StepVerifier.create(orchestrator.findById("task-nonexist"))
                    .verifyComplete();
        }
    }

    @Nested
    @DisplayName("handleStepProgress")
    class HandleStepProgress {

        @Test
        @DisplayName("should ignore progress for terminal tasks")
        void shouldIgnoreTerminalTaskProgress() {
            Task completed = new Task();
            completed.setId("task-done");
            completed.setStatus(TaskStatus.COMPLETED.name());

            when(taskRepo.findById("task-done")).thenReturn(Mono.just(completed));

            StepVerifier.create(orchestrator.handleStepProgress(
                            "task-done", "PARSE", "SUCCESS", Map.of()))
                    .assertNext(t -> assertEquals(TaskStatus.COMPLETED.name(), t.getStatus()))
                    .verifyComplete();

            verify(stepRepo, never()).findByTaskIdAndStepName(anyString(), anyString());
        }

        @Test
        @DisplayName("should persist each retry and fail only after the third retry is exhausted")
        void shouldPersistEachRetryAndFailOnlyAfterThirdRetryIsExhausted() {
            Task task = new Task();
            task.setId("task-retry");
            task.setStatus(TaskStatus.PARSE_RUNNING.name());
            task.setPayload("{\"pdfObjectKey\":\"reports/retry.pdf\"}");
            TaskStep step = new TaskStep();
            step.setTaskId(task.getId());
            step.setStepName("PARSE");
            step.setStatus(StepStatus.RUNNING.name());
            step.setRetryCount(0);

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "PARSE")).thenReturn(Mono.just(step));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));

            for (int expectedRetry = 1; expectedRetry <= 3; expectedRetry++) {
                final int retry = expectedRetry;
                StepVerifier.create(orchestrator.handleStepProgress(
                                task.getId(), "PARSE", "FAILED", Map.of("error", "AI timeout"))
                                .contextWrite(context -> context.put(TraceContext.TRACE_ID, "retry-trace")))
                        .assertNext(saved -> assertEquals(TaskStatus.PARSE_RETRY.name(), saved.getStatus()))
                        .verifyComplete();
                assertEquals(retry, step.getRetryCount());
                assertEquals(StepStatus.RETRY.name(), step.getStatus());
                verify(messageProducer).publishRetry(
                        eq(task.getId()), eq("parse"), any(Map.class), eq(retry), eq("retry-trace"));
            }

            StepVerifier.create(orchestrator.handleStepProgress(
                            task.getId(), "PARSE", "FAILED", Map.of("error", "AI timeout")))
                    .assertNext(saved -> assertEquals(TaskStatus.FAILED.name(), saved.getStatus()))
                    .verifyComplete();

            assertEquals(StepStatus.FAILED.name(), step.getStatus());
            verify(messageProducer, times(3)).publishRetry(
                    eq(task.getId()), eq("parse"), any(Map.class), any(Integer.class), anyString());
        }

        @Test
        @DisplayName("should ignore duplicate successful progress without dispatching the next step")
        void shouldIgnoreDuplicateSuccessfulProgressWithoutDispatchingNextStep() {
            Task task = new Task();
            task.setId("task-duplicate-success");
            task.setStatus(TaskStatus.PARSE_SUCCESS.name());
            TaskStep step = new TaskStep();
            step.setTaskId(task.getId());
            step.setStepName("PARSE");
            step.setStatus(StepStatus.SUCCESS.name());

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "PARSE")).thenReturn(Mono.just(step));

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "PARSE", "SUCCESS", Map.of()))
                    .expectNext(task)
                    .verifyComplete();

            verify(stepRepo, never()).save(any(TaskStep.class));
            verify(taskRepo, never()).save(any(Task.class));
            verify(messageProducer, never()).publishTaskStep(anyString(), anyString(), any(Map.class), anyString());
        }

        @Test
        @DisplayName("should throw for nonexistent task")
        void shouldThrowForNonexistentTask() {
            when(taskRepo.findById("task-ghost")).thenReturn(Mono.empty());

            StepVerifier.create(orchestrator.handleStepProgress(
                            "task-ghost", "PARSE", "SUCCESS", Map.of()))
                    .expectErrorMatches(ex -> ex instanceof com.finreport.exception.BusinessException
                            && "TASK_NOT_FOUND".equals(
                            ((com.finreport.exception.BusinessException) ex).getErrorCode()))
                    .verify();
        }
    }

    @Nested
    @DisplayName("cancelTask")
    class CancelTask {

        @Test
        @DisplayName("should cancel a running task")
        void shouldCancelRunningTask() {
            Task running = new Task();
            running.setId("task-run");
            running.setStatus(TaskStatus.PARSE_RUNNING.name());

            when(taskRepo.findById("task-run")).thenReturn(Mono.just(running));
            when(taskRepo.save(any(Task.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));

            StepVerifier.create(orchestrator.cancelTask("task-run"))
                    .assertNext(t -> assertEquals(TaskStatus.CANCELLED.name(), t.getStatus()))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should not cancel a completed task")
        void shouldNotCancelCompletedTask() {
            Task completed = new Task();
            completed.setId("task-done");
            completed.setStatus(TaskStatus.COMPLETED.name());

            when(taskRepo.findById("task-done")).thenReturn(Mono.just(completed));

            StepVerifier.create(orchestrator.cancelTask("task-done"))
                    .assertNext(t -> assertEquals(TaskStatus.COMPLETED.name(), t.getStatus()))
                    .verifyComplete();

            verify(taskRepo, never()).save(any());
        }

        @Test
        @DisplayName("should throw when cancelling non-cancellable state")
        void shouldRejectCancelOnNonCancellable() {
            // FAILED is terminal but not cancellable
            Task failed = new Task();
            failed.setId("task-fail");
            failed.setStatus(TaskStatus.FAILED.name());
            when(taskRepo.findById("task-fail")).thenReturn(Mono.just(failed));

            StepVerifier.create(orchestrator.cancelTask("task-fail"))
                    .assertNext(t -> assertEquals(TaskStatus.FAILED.name(), t.getStatus()))
                    .verifyComplete();
        }
    }

    @Nested
    @DisplayName("reliability and ownership branches")
    class ReliabilityAndOwnershipBranches {

        @Test
        @DisplayName("should update a task report reference and hide missing tasks")
        void shouldUpdateReportReferenceAndRejectMissingTask() {
            Task task = Task.builder().id("task-report-ref").status(TaskStatus.PENDING.name()).build();
            when(taskRepo.findById("task-report-ref")).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));

            StepVerifier.create(orchestrator.updateRefReportId("task-report-ref", 88L))
                    .assertNext(saved -> assertEquals(88L, saved.getRefReportId()))
                    .verifyComplete();

            when(taskRepo.findById("task-report-missing")).thenReturn(Mono.empty());
            StepVerifier.create(orchestrator.updateRefReportId("task-report-missing", 88L))
                    .expectErrorMatches(error -> error instanceof com.finreport.exception.BusinessException businessException
                            && "TASK_NOT_FOUND".equals(businessException.getErrorCode()))
                    .verify();
        }

        @Test
        @DisplayName("should mark only non-terminal tasks as failed")
        void shouldMarkOnlyNonTerminalTasksAsFailed() {
            Task active = Task.builder().id("task-active-fail").status(TaskStatus.PARSE_RUNNING.name()).build();
            Task completed = Task.builder().id("task-completed").status(TaskStatus.COMPLETED.name()).build();
            when(taskRepo.findById("task-active-fail")).thenReturn(Mono.just(active));
            when(taskRepo.findById("task-completed")).thenReturn(Mono.just(completed));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));

            StepVerifier.create(orchestrator.markTaskFailed("task-active-fail", "upload failed"))
                    .assertNext(saved -> {
                        assertEquals(TaskStatus.FAILED.name(), saved.getStatus());
                        assertEquals("upload failed", saved.getErrorMsg());
                        assertNotNull(saved.getFinishedAt());
                    })
                    .verifyComplete();
            StepVerifier.create(orchestrator.markTaskFailed("task-completed", "ignored"))
                    .expectNext(completed)
                    .verifyComplete();

            verify(taskRepo, times(1)).save(any(Task.class));
        }

        @Test
        @DisplayName("should keep scoped query and scoped cancel within the owner boundary")
        void shouldUseScopedRepositoryQueriesForOwnerBoundary() {
            Task owned = Task.builder().id("task-owned").userId(7L).status(TaskStatus.PENDING.name()).build();
            when(taskRepo.findByIdAndUserId("task-owned", 7L)).thenReturn(Mono.just(owned));
            when(taskRepo.findByIdAndUserId("task-hidden", 8L)).thenReturn(Mono.empty());

            StepVerifier.create(orchestrator.findByIdAndUserId("task-owned", 7L))
                    .expectNext(owned)
                    .verifyComplete();
            StepVerifier.create(orchestrator.cancelTask("task-hidden", 8L))
                    .expectErrorMatches(error -> error instanceof com.finreport.exception.BusinessException businessException
                            && "TASK_NOT_FOUND".equals(businessException.getErrorCode()))
                    .verify();
        }

        @Test
        @DisplayName("should leave an unknown progress status unchanged")
        void shouldLeaveUnknownProgressStatusUnchanged() {
            Task task = Task.builder().id("task-unknown-progress").status(TaskStatus.PARSE_RUNNING.name()).build();
            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "PARSE", "RUNNING", Map.of()))
                    .expectNext(task)
                    .verifyComplete();
        }

        @Test
        @DisplayName("should complete the task after the REPORT step succeeds")
        void shouldCompleteTaskAfterReportSuccess() {
            Task task = Task.builder().id("task-report-success").status(TaskStatus.REPORT_RUNNING.name())
                    .progress(75).payload("{\"pdfObjectKey\":\"uploads/7/report.pdf\"}").build();
            TaskStep step = TaskStep.builder().taskId(task.getId()).stepName("REPORT")
                    .status(StepStatus.RUNNING.name()).build();
            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "REPORT")).thenReturn(Mono.just(step));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "REPORT", "SUCCESS", Map.of()))
                    .assertNext(saved -> {
                        assertEquals(TaskStatus.COMPLETED.name(), saved.getStatus());
                        assertEquals(100, saved.getProgress());
                        assertNotNull(saved.getFinishedAt());
                    })
                    .verifyComplete();
        }

        @Test
        @DisplayName("should retain EXTRACT_PARTIAL until every extraction step succeeds")
        void shouldRetainPartialExtractionUntilAllStepsSucceed() {
            Task task = Task.builder().id("task-extract-partial").status(TaskStatus.EXTRACT_RUNNING.name()).build();
            TaskStep bs = TaskStep.builder().taskId(task.getId()).stepName("EXTRACT_BS")
                    .status(StepStatus.RUNNING.name()).build();
            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_BS")).thenReturn(Mono.just(bs));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_IS")).thenReturn(Mono.empty());
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "EXTRACT_BS", "SUCCESS", Map.of()))
                    .assertNext(saved -> assertEquals(TaskStatus.EXTRACT_PARTIAL.name(), saved.getStatus()))
                    .verifyComplete();
            verify(messageProducer, never()).publishTaskStep(anyString(), anyString(), any(Map.class), anyString());
        }
    }

    @Nested
    @DisplayName("task status value verification")
    class TaskStatusValues {

        @Test
        @DisplayName("all 21 states should have valid enum names")
        void shouldHaveAllStates() {
            assertEquals("PENDING", TaskStatus.PENDING.name());
            assertEquals("COMPLETED", TaskStatus.COMPLETED.name());
            assertEquals("FAILED", TaskStatus.FAILED.name());
            assertEquals("CANCELLED", TaskStatus.CANCELLED.name());
            assertEquals("EXTRACT_PARTIAL", TaskStatus.EXTRACT_PARTIAL.name());
        }
    }
}
