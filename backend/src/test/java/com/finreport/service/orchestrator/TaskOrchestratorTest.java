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
import static org.mockito.Mockito.lenient;
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

import com.finreport.domain.entity.Report;
import com.finreport.domain.entity.Task;
import com.finreport.domain.entity.TaskStep;
import com.finreport.domain.enums.StepStatus;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.domain.enums.TaskStepName;
import com.finreport.exception.IntegrationException;
import com.finreport.mq.TaskMessageProducer;
import com.finreport.repository.ReportRepository;
import com.finreport.repository.TaskRepository;
import com.finreport.repository.TaskStepRepository;
import com.finreport.service.statement.StatementWriter;
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

    @Mock
    private ExtractDispatcher extractDispatcher;

    @Mock
    private ExtractCompletionTracker extractTracker;

    @Mock
    private StatementWriter statementWriter;

    @Mock
    private ExtractCacheService extractCacheService;

    @Mock
    private ReportRepository reportRepo;

    private TaskStateMachine stateMachine;
    private TaskOrchestrator orchestrator;

    @BeforeEach
    void setUp() {
        stateMachine = new TaskStateMachine();
        orchestrator = new TaskOrchestrator(
                taskRepo, stepRepo, stateMachine, messageProducer, databaseClient, transactionalOperator,
                extractDispatcher, extractTracker, statementWriter, extractCacheService, reportRepo);
        // M2.09: StatementWriter returns 0 by default; individual tests override when needed.
        lenient().when(statementWriter.writeStatement(anyString(), anyString(), any()))
                .thenReturn(Mono.just(0));
        // M2.10: Cache miss by default; individual tests override for cache-hit scenarios.
        lenient().when(reportRepo.findByTaskId(anyString())).thenReturn(Mono.empty());
        lenient().when(extractCacheService.lookupAll(anyString())).thenReturn(Mono.just(java.util.Map.of()));
        lenient().when(extractCacheService.store(anyString(), any(TaskStepName.class), any()))
                .thenReturn(Mono.empty());
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
        @DisplayName("should reconcile a replayed PARSE success by dispatching missing extraction steps")
        void shouldReconcileReplayedParseSuccessByDispatchingMissingExtractionSteps() {
            Task task = Task.builder()
                    .id("task-duplicate-success")
                    .status(TaskStatus.PARSE_SUCCESS.name())
                    .payload("{\"pdfObjectKey\":\"uploads/replay.pdf\"}")
                    .build();
            TaskStep parse = TaskStep.builder().taskId(task.getId()).stepName("PARSE")
                    .status(StepStatus.SUCCESS.name()).build();

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "PARSE")).thenReturn(Mono.just(parse));
            // M2.08: extraction dispatch delegated to ExtractDispatcher
            when(extractDispatcher.dispatchAll(any(Task.class), any(Map.class))).thenReturn(Mono.empty());

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "PARSE", "SUCCESS", Map.of()))
                    .assertNext(saved -> assertEquals(TaskStatus.EXTRACT_RUNNING.name(), saved.getStatus()))
                    .verifyComplete();

            verify(extractDispatcher).dispatchAll(any(Task.class), any(Map.class));
            verify(messageProducer, never()).publishTaskStep(
                    anyString(), anyString(), any(Map.class), anyString());
        }

        @Test
        @DisplayName("should not rewind a later task phase when an extraction success is replayed")
        void shouldNotRewindLaterTaskPhaseWhenExtractionSuccessIsReplayed() {
            Task task = Task.builder().id("task-later-phase").status(TaskStatus.CHECK_RUNNING.name()).build();
            TaskStep balanceSheet = TaskStep.builder().taskId(task.getId()).stepName("EXTRACT_BS")
                    .status(StepStatus.SUCCESS.name()).build();
            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_BS")).thenReturn(Mono.just(balanceSheet));

            StepVerifier.create(orchestrator.handleStepProgress(
                            task.getId(), "EXTRACT_BS", "SUCCESS", Map.of()))
                    .assertNext(saved -> assertEquals(TaskStatus.CHECK_RUNNING.name(), saved.getStatus()))
                    .verifyComplete();

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
        @DisplayName("should dispatch all extraction steps when PARSE succeeds")
        void shouldDispatchAllExtractionStepsWhenParseSucceeds() {
            Task task = Task.builder()
                    .id("task-parallel-extraction")
                    .status(TaskStatus.PARSE_RUNNING.name())
                    .payload("{\"pdfObjectKey\":\"uploads/7/report.pdf\"}")
                    .build();
            TaskStep parse = TaskStep.builder().taskId(task.getId()).stepName("PARSE")
                    .status(StepStatus.RUNNING.name()).build();

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "PARSE")).thenReturn(Mono.just(parse));
            // M2.08: extraction dispatch delegated to ExtractDispatcher
            when(extractDispatcher.dispatchAll(any(Task.class), any(Map.class))).thenReturn(Mono.empty());

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "PARSE", "SUCCESS", Map.of())
                            .contextWrite(context -> context.put(TraceContext.TRACE_ID, "parallel-trace")))
                    .assertNext(saved -> {
                        assertEquals(TaskStatus.EXTRACT_RUNNING.name(), saved.getStatus());
                        assertEquals("EXTRACT_BS", saved.getCurrentStep());
                    })
                    .verifyComplete();

            verify(extractDispatcher).dispatchAll(any(Task.class), any(Map.class));
            // TaskOrchestrator no longer publishes extract messages directly (delegated to ExtractDispatcher)
            verify(messageProducer, never()).publishTaskStep(
                    eq(task.getId()), eq("extract.bs"), any(Map.class), anyString());
        }

        @Test
        @DisplayName("should propagate IntegrationException when extract dispatch fails")
        void shouldPropagateIntegrationExceptionWhenExtractDispatchFails() {
            Task task = Task.builder()
                    .id("task-extraction-publish-failure")
                    .status(TaskStatus.PARSE_RUNNING.name())
                    .payload("{\"pdfObjectKey\":\"uploads/7/report.pdf\"}")
                    .build();
            TaskStep parse = TaskStep.builder().taskId(task.getId()).stepName("PARSE")
                    .status(StepStatus.RUNNING.name()).build();
            IntegrationException mqError = new IntegrationException(
                    org.springframework.http.HttpStatus.SERVICE_UNAVAILABLE,
                    "MQ_PUBLISH_FAILED", "RabbitMQ is unavailable");

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "PARSE")).thenReturn(Mono.just(parse));
            // M2.08: ExtractDispatcher internally handles MQ failure via markDispatchFailed;
            // TaskOrchestrator just propagates the IntegrationException.
            when(extractDispatcher.dispatchAll(any(Task.class), any(Map.class))).thenReturn(Mono.error(mqError));

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "PARSE", "SUCCESS", Map.of())
                            .contextWrite(context -> context.put(TraceContext.TRACE_ID, "failure-trace")))
                    .expectErrorMatches(error -> error instanceof IntegrationException integrationException
                            && "MQ_PUBLISH_FAILED".equals(integrationException.getErrorCode()))
                    .verify();

            // task transitioned to EXTRACT_RUNNING before dispatcher failed;
            // FAILED transition is dispatched inside ExtractDispatcher (covered by ExtractDispatcherTest)
            assertEquals(TaskStatus.EXTRACT_RUNNING.name(), task.getStatus());
            verify(extractDispatcher).dispatchAll(any(Task.class), any(Map.class));
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
            // Flux.all 在 IS 处短路（BS 已 SUCCESS, IS 空 → MISSING → predicate false），
            // CF 的 stub 可能未被订阅 → 用 lenient 避免 UnnecessaryStubbing 报错。
            lenient().when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_CF"))
                    .thenReturn(Mono.empty());
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            // M2.08: Redis hot path returns count < EXPECTED_COUNT so we fall back to MySQL reconcile
            when(extractTracker.recordSuccess(eq(task.getId()), any(TaskStepName.class))).thenReturn(Mono.just(1));
            when(extractTracker.hasFailed(task.getId())).thenReturn(Mono.just(false));

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "EXTRACT_BS", "SUCCESS", Map.of()))
                    .assertNext(saved -> assertEquals(TaskStatus.EXTRACT_PARTIAL.name(), saved.getStatus()))
                    .verifyComplete();
            verify(messageProducer, never()).publishTaskStep(anyString(), anyString(), any(Map.class), anyString());
            verify(extractDispatcher, never()).dispatchAll(any(Task.class), any(Map.class));
        }

        @Test
        @DisplayName("should transition to EXTRACT_SUCCESS via Redis hot path when third extract succeeds")
        void shouldTransitionToExtractSuccessViaRedisHotPathWhenThirdExtractSucceeds() {
            Task task = Task.builder().id("task-extract-third").status(TaskStatus.EXTRACT_RUNNING.name()).build();
            TaskStep cf = TaskStep.builder().taskId(task.getId()).stepName("EXTRACT_CF")
                    .status(StepStatus.RUNNING.name()).build();
            TaskStep check = TaskStep.builder().taskId(task.getId()).stepName("CHECK")
                    .status(StepStatus.PENDING.name()).build();

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_CF")).thenReturn(Mono.just(cf));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "CHECK")).thenReturn(Mono.just(check));
            // M2.08: Redis hot path returns count == EXPECTED_COUNT and no failed flag
            when(extractTracker.recordSuccess(eq(task.getId()), any(TaskStepName.class))).thenReturn(Mono.just(3));
            when(extractTracker.hasFailed(task.getId())).thenReturn(Mono.just(false));

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "EXTRACT_CF", "SUCCESS", Map.of()))
                    .assertNext(saved -> assertEquals(TaskStatus.CHECK_RUNNING.name(), saved.getStatus()))
                    .verifyComplete();

            // CHECK dispatched via dispatchStep (which calls messageProducer.publishTaskStep directly)
            verify(messageProducer).publishTaskStep(
                    eq(task.getId()), eq("check"), any(Map.class), anyString());
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

    @Nested
    @DisplayName("M2.10 extraction cache")
    class ExtractionCache {

        @Test
        @DisplayName("should skip extract MQ and replay cached results when all 3 cached")
        void shouldSkipExtractAndReplayWhenAllCached() {
            // setup: task in PARSE_RUNNING, report has pdfMd5, cache has 3 entries
            Task task = Task.builder()
                    .id("task-cache-hit")
                    .userId(1L)
                    .status(TaskStatus.PARSE_RUNNING.name())
                    .payload("{\"pdfObjectKey\":\"uploads/test.pdf\"}")
                    .build();
            Report report = Report.builder()
                    .id(10L)
                    .taskId(task.getId())
                    .pdfMd5("md5-cache-hit")
                    .build();
            TaskStep parse = TaskStep.builder().taskId(task.getId()).stepName("PARSE")
                    .status(StepStatus.RUNNING.name()).build();
            TaskStep bs = TaskStep.builder().taskId(task.getId()).stepName("EXTRACT_BS")
                    .status(StepStatus.PENDING.name()).build();
            TaskStep is = TaskStep.builder().taskId(task.getId()).stepName("EXTRACT_IS")
                    .status(StepStatus.PENDING.name()).build();
            TaskStep cf = TaskStep.builder().taskId(task.getId()).stepName("EXTRACT_CF")
                    .status(StepStatus.PENDING.name()).build();
            TaskStep check = TaskStep.builder().taskId(task.getId()).stepName("CHECK")
                    .status(StepStatus.PENDING.name()).build();

            Map<String, Object> bsResult = Map.of("success", true, "statement", Map.of());
            Map<String, Object> isResult = Map.of("success", true, "statement", Map.of());
            Map<String, Object> cfResult = Map.of("success", true, "statement", Map.of());
            Map<TaskStepName, Map<String, Object>> cached = Map.of(
                    TaskStepName.EXTRACT_BS, bsResult,
                    TaskStepName.EXTRACT_IS, isResult,
                    TaskStepName.EXTRACT_CF, cfResult);

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "PARSE")).thenReturn(Mono.just(parse));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_BS")).thenReturn(Mono.just(bs));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_IS")).thenReturn(Mono.just(is));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_CF")).thenReturn(Mono.just(cf));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "CHECK")).thenReturn(Mono.just(check));
            when(reportRepo.findByTaskId(task.getId())).thenReturn(Mono.just(report));
            when(extractCacheService.lookupAll("md5-cache-hit")).thenReturn(Mono.just(cached));
            when(statementWriter.writeStatement(anyString(), anyString(), any())).thenReturn(Mono.just(5));

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "PARSE", "SUCCESS", Map.of())
                            .contextWrite(ctx -> ctx.put(TraceContext.TRACE_ID, "cache-trace")))
                    .assertNext(saved -> assertEquals(TaskStatus.CHECK_RUNNING.name(), saved.getStatus()))
                    .verifyComplete();

            // No extract MQ dispatched
            verify(messageProducer, never()).publishTaskStep(
                    eq(task.getId()), eq("extract.bs"), any(Map.class), anyString());
            verify(messageProducer, never()).publishTaskStep(
                    eq(task.getId()), eq("extract.is"), any(Map.class), anyString());
            verify(messageProducer, never()).publishTaskStep(
                    eq(task.getId()), eq("extract.cf"), any(Map.class), anyString());
            // No extract dispatch (cache hit)
            verify(extractDispatcher, never()).dispatchAll(any(Task.class), any(Map.class));
            // CHECK dispatched
            verify(messageProducer).publishTaskStep(
                    eq(task.getId()), eq("check"), any(Map.class), anyString());
            // All 3 statements written
            verify(statementWriter).writeStatement(task.getId(), "EXTRACT_BS", bsResult);
            verify(statementWriter).writeStatement(task.getId(), "EXTRACT_IS", isResult);
            verify(statementWriter).writeStatement(task.getId(), "EXTRACT_CF", cfResult);
            // All 3 steps marked SUCCESS
            assertEquals(StepStatus.SUCCESS.name(), bs.getStatus());
            assertEquals(StepStatus.SUCCESS.name(), is.getStatus());
            assertEquals(StepStatus.SUCCESS.name(), cf.getStatus());
        }

        @Test
        @DisplayName("should fall through to extract dispatch when cache miss")
        void shouldFallThroughToExtractDispatchWhenCacheMiss() {
            Task task = Task.builder()
                    .id("task-cache-miss")
                    .userId(1L)
                    .status(TaskStatus.PARSE_RUNNING.name())
                    .payload("{\"pdfObjectKey\":\"uploads/test.pdf\"}")
                    .build();
            Report report = Report.builder()
                    .id(11L)
                    .taskId(task.getId())
                    .pdfMd5("md5-cache-miss")
                    .build();
            TaskStep parse = TaskStep.builder().taskId(task.getId()).stepName("PARSE")
                    .status(StepStatus.RUNNING.name()).build();

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "PARSE")).thenReturn(Mono.just(parse));
            when(reportRepo.findByTaskId(task.getId())).thenReturn(Mono.just(report));
            // Cache miss: empty Map
            when(extractCacheService.lookupAll("md5-cache-miss")).thenReturn(Mono.just(Map.of()));
            when(extractDispatcher.dispatchAll(any(Task.class), any(Map.class))).thenReturn(Mono.empty());

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "PARSE", "SUCCESS", Map.of())
                            .contextWrite(ctx -> ctx.put(TraceContext.TRACE_ID, "miss-trace")))
                    .assertNext(saved -> assertEquals(TaskStatus.EXTRACT_RUNNING.name(), saved.getStatus()))
                    .verifyComplete();

            verify(extractDispatcher).dispatchAll(any(Task.class), any(Map.class));
        }

        @Test
        @DisplayName("should fall through to extract dispatch when only partial cache hit")
        void shouldFallThroughToExtractDispatchWhenPartialCacheHit() {
            Task task = Task.builder()
                    .id("task-partial-cache")
                    .userId(1L)
                    .status(TaskStatus.PARSE_RUNNING.name())
                    .payload("{\"pdfObjectKey\":\"uploads/test.pdf\"}")
                    .build();
            Report report = Report.builder()
                    .id(12L)
                    .taskId(task.getId())
                    .pdfMd5("md5-partial")
                    .build();
            TaskStep parse = TaskStep.builder().taskId(task.getId()).stepName("PARSE")
                    .status(StepStatus.RUNNING.name()).build();

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "PARSE")).thenReturn(Mono.just(parse));
            when(reportRepo.findByTaskId(task.getId())).thenReturn(Mono.just(report));
            // Partial: only BS cached
            when(extractCacheService.lookupAll("md5-partial")).thenReturn(Mono.just(
                    Map.of(TaskStepName.EXTRACT_BS, Map.of("success", true))));
            when(extractDispatcher.dispatchAll(any(Task.class), any(Map.class))).thenReturn(Mono.empty());

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "PARSE", "SUCCESS", Map.of())
                            .contextWrite(ctx -> ctx.put(TraceContext.TRACE_ID, "partial-trace")))
                    .assertNext(saved -> assertEquals(TaskStatus.EXTRACT_RUNNING.name(), saved.getStatus()))
                    .verifyComplete();

            verify(extractDispatcher).dispatchAll(any(Task.class), any(Map.class));
        }

        @Test
        @DisplayName("should fall through to extract dispatch when report not found")
        void shouldFallThroughToExtractDispatchWhenReportNotFound() {
            Task task = Task.builder()
                    .id("task-no-report")
                    .userId(1L)
                    .status(TaskStatus.PARSE_RUNNING.name())
                    .payload("{\"pdfObjectKey\":\"uploads/test.pdf\"}")
                    .build();
            TaskStep parse = TaskStep.builder().taskId(task.getId()).stepName("PARSE")
                    .status(StepStatus.RUNNING.name()).build();

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "PARSE")).thenReturn(Mono.just(parse));
            // Report lookup returns empty (lenient default)
            when(extractDispatcher.dispatchAll(any(Task.class), any(Map.class))).thenReturn(Mono.empty());

            StepVerifier.create(orchestrator.handleStepProgress(task.getId(), "PARSE", "SUCCESS", Map.of())
                            .contextWrite(ctx -> ctx.put(TraceContext.TRACE_ID, "no-report-trace")))
                    .assertNext(saved -> assertEquals(TaskStatus.EXTRACT_RUNNING.name(), saved.getStatus()))
                    .verifyComplete();

            verify(extractCacheService, never()).lookupAll(anyString());
            verify(extractDispatcher).dispatchAll(any(Task.class), any(Map.class));
        }

        @Test
        @DisplayName("should store extract result to cache after writeStatement on EXTRACT success")
        void shouldStoreExtractResultToCacheAfterWriteStatementOnExtractSuccess() {
            Task task = Task.builder()
                    .id("task-store-cache")
                    .userId(1L)
                    .status(TaskStatus.EXTRACT_RUNNING.name())
                    .build();
            Report report = Report.builder()
                    .id(13L)
                    .taskId(task.getId())
                    .pdfMd5("md5-store")
                    .build();
            TaskStep bs = TaskStep.builder().taskId(task.getId()).stepName("EXTRACT_BS")
                    .status(StepStatus.RUNNING.name()).build();

            Map<String, Object> result = Map.of("success", true, "statement", Map.of());

            when(taskRepo.findById(task.getId())).thenReturn(Mono.just(task));
            when(taskRepo.save(any(Task.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(stepRepo.save(any(TaskStep.class))).thenAnswer(inv -> Mono.just(inv.getArgument(0)));
            when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_BS")).thenReturn(Mono.just(bs));
            // IS/CF not yet SUCCESS → Flux.all short-circuits
            lenient().when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_IS"))
                    .thenReturn(Mono.empty());
            lenient().when(stepRepo.findByTaskIdAndStepName(task.getId(), "EXTRACT_CF"))
                    .thenReturn(Mono.empty());
            when(extractTracker.recordSuccess(eq(task.getId()), any(TaskStepName.class)))
                    .thenReturn(Mono.just(1));
            when(extractTracker.hasFailed(task.getId())).thenReturn(Mono.just(false));
            when(statementWriter.writeStatement(task.getId(), "EXTRACT_BS", result))
                    .thenReturn(Mono.just(5));
            when(reportRepo.findByTaskId(task.getId())).thenReturn(Mono.just(report));
            when(extractCacheService.store("md5-store", TaskStepName.EXTRACT_BS, result))
                    .thenReturn(Mono.empty());

            StepVerifier.create(orchestrator.handleStepProgress(
                            task.getId(), "EXTRACT_BS", "SUCCESS", result))
                    .assertNext(saved -> assertEquals(TaskStatus.EXTRACT_PARTIAL.name(), saved.getStatus()))
                    .verifyComplete();

            // Cache store called with resolved pdfMd5 + step + result
            verify(extractCacheService).store("md5-store", TaskStepName.EXTRACT_BS, result);
        }
    }
}
