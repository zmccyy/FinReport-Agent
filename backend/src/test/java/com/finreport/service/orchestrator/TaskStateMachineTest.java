package com.finreport.service.orchestrator;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import com.finreport.domain.enums.TaskStatus;

/**
 * TaskStateMachine 单元测试 — 覆盖 spec §3.2.1 所有合法和不合法转换。
 */
@DisplayName("TaskStateMachine")
class TaskStateMachineTest {

    private TaskStateMachine sm;

    @BeforeEach
    void setUp() {
        sm = new TaskStateMachine();
    }

    @Nested
    @DisplayName("初始状态转换")
    class InitialTransitions {

        @Test
        @DisplayName("PENDING → PARSE_RUNNING 合法")
        void shouldAllowPendingToParseRunning() {
            assertTrue(sm.canTransition(TaskStatus.PENDING, TaskStatus.PARSE_RUNNING));
        }

        @Test
        @DisplayName("PENDING → CANCELLED 合法（用户取消）")
        void shouldAllowPendingToCancelled() {
            assertTrue(sm.canTransition(TaskStatus.PENDING, TaskStatus.CANCELLED));
        }

        @Test
        @DisplayName("PENDING → FAILED 不合法（不能直接失败）")
        void shouldNotAllowPendingToFailed() {
            assertFalse(sm.canTransition(TaskStatus.PENDING, TaskStatus.FAILED));
        }
    }

    @Nested
    @DisplayName("解析阶段转换")
    class ParseTransitions {

        @Test
        @DisplayName("PARSE_RUNNING → PARSE_SUCCESS 合法")
        void shouldAllowParseRunningToSuccess() {
            assertTrue(sm.canTransition(TaskStatus.PARSE_RUNNING, TaskStatus.PARSE_SUCCESS));
        }

        @Test
        @DisplayName("PARSE_RUNNING → PARSE_FAILED 合法")
        void shouldAllowParseRunningToFailed() {
            assertTrue(sm.canTransition(TaskStatus.PARSE_RUNNING, TaskStatus.PARSE_FAILED));
        }

        @Test
        @DisplayName("PARSE_RUNNING → CANCELLED 合法")
        void shouldAllowParseRunningToCancelled() {
            assertTrue(sm.canTransition(TaskStatus.PARSE_RUNNING, TaskStatus.CANCELLED));
        }

        @Test
        @DisplayName("PARSE_FAILED 在重试次数 < MAX_RETRIES 时进入 PARSE_RETRY")
        void shouldEnterRetryWhenUnderLimit() {
            assertEquals(TaskStatus.PARSE_RETRY,
                    sm.decideRetryOrFail(TaskStatus.PARSE_FAILED, 0));
        }

        @Test
        @DisplayName("PARSE_FAILED 在重试次数 >= MAX_RETRIES 时进入 FAILED")
        void shouldEnterFailedWhenRetriesExhausted() {
            assertEquals(TaskStatus.FAILED,
                    sm.decideRetryOrFail(TaskStatus.PARSE_FAILED, TaskStateMachine.MAX_RETRIES));
            assertEquals(TaskStatus.FAILED,
                    sm.decideRetryOrFail(TaskStatus.PARSE_FAILED, 5));
        }

        @Test
        @DisplayName("PARSE_RETRY → PARSE_RUNNING 合法")
        void shouldAllowParseRetryToRunning() {
            assertTrue(sm.canTransition(TaskStatus.PARSE_RETRY, TaskStatus.PARSE_RUNNING));
        }

        @Test
        @DisplayName("PARSE_SUCCESS → EXTRACT_RUNNING 合法")
        void shouldAllowParseSuccessToExtractRunning() {
            assertTrue(sm.canTransition(TaskStatus.PARSE_SUCCESS, TaskStatus.EXTRACT_RUNNING));
        }

        @Test
        @DisplayName("PARSE_SUCCESS → CHECK_RUNNING 不合法（跳步）")
        void shouldNotSkipExtract() {
            assertFalse(sm.canTransition(TaskStatus.PARSE_SUCCESS, TaskStatus.CHECK_RUNNING));
        }
    }

    @Nested
    @DisplayName("抽取阶段转换（三表并行）")
    class ExtractTransitions {

        @Test
        @DisplayName("EXTRACT_RUNNING → EXTRACT_PARTIAL 合法")
        void shouldAllowExtractRunningToPartial() {
            assertTrue(sm.canTransition(TaskStatus.EXTRACT_RUNNING, TaskStatus.EXTRACT_PARTIAL));
        }

        @Test
        @DisplayName("EXTRACT_RUNNING → EXTRACT_SUCCESS 合法（三条同时完成）")
        void shouldAllowExtractRunningToSuccess() {
            assertTrue(sm.canTransition(TaskStatus.EXTRACT_RUNNING, TaskStatus.EXTRACT_SUCCESS));
        }

        @Test
        @DisplayName("EXTRACT_PARTIAL → EXTRACT_SUCCESS 合法")
        void shouldAllowExtractPartialToSuccess() {
            assertTrue(sm.canTransition(TaskStatus.EXTRACT_PARTIAL, TaskStatus.EXTRACT_SUCCESS));
        }

        @Test
        @DisplayName("EXTRACT_PARTIAL → EXTRACT_FAILED 合法")
        void shouldAllowExtractPartialToFailed() {
            assertTrue(sm.canTransition(TaskStatus.EXTRACT_PARTIAL, TaskStatus.EXTRACT_FAILED));
        }

        @Test
        @DisplayName("EXTRACT_FAILED 重试 < MAX_RETRIES → EXTRACT_RETRY")
        void shouldRetryExtract() {
            assertEquals(TaskStatus.EXTRACT_RETRY,
                    sm.decideRetryOrFail(TaskStatus.EXTRACT_FAILED, 0));
        }

        @Test
        @DisplayName("EXTRACT_SUCCESS → CHECK_RUNNING 合法")
        void shouldAllowExtractSuccessToCheckRunning() {
            assertTrue(sm.canTransition(TaskStatus.EXTRACT_SUCCESS, TaskStatus.CHECK_RUNNING));
        }
    }

    @Nested
    @DisplayName("勾稽阶段转换")
    class CheckTransitions {

        @Test
        @DisplayName("CHECK_RUNNING → CHECK_SUCCESS 合法")
        void shouldAllowCheckRunningToSuccess() {
            assertTrue(sm.canTransition(TaskStatus.CHECK_RUNNING, TaskStatus.CHECK_SUCCESS));
        }

        @Test
        @DisplayName("CHECK_FAILED 重试 < MAX_RETRIES → CHECK_RETRY")
        void shouldRetryCheck() {
            assertEquals(TaskStatus.CHECK_RETRY,
                    sm.decideRetryOrFail(TaskStatus.CHECK_FAILED, 0));
        }

        @Test
        @DisplayName("CHECK_SUCCESS → REPORT_RUNNING 合法")
        void shouldAllowCheckSuccessToReportRunning() {
            assertTrue(sm.canTransition(TaskStatus.CHECK_SUCCESS, TaskStatus.REPORT_RUNNING));
        }
    }

    @Nested
    @DisplayName("报告阶段转换")
    class ReportTransitions {

        @Test
        @DisplayName("REPORT_RUNNING → REPORT_SUCCESS 合法")
        void shouldAllowReportRunningToSuccess() {
            assertTrue(sm.canTransition(TaskStatus.REPORT_RUNNING, TaskStatus.REPORT_SUCCESS));
        }

        @Test
        @DisplayName("REPORT_SUCCESS → COMPLETED 合法")
        void shouldAllowReportSuccessToCompleted() {
            assertTrue(sm.canTransition(TaskStatus.REPORT_SUCCESS, TaskStatus.COMPLETED));
        }

        @Test
        @DisplayName("REPORT_FAILED 重试耗尽 → FAILED")
        void shouldFailWhenReportRetriesExhausted() {
            assertEquals(TaskStatus.FAILED,
                    sm.decideRetryOrFail(TaskStatus.REPORT_FAILED, TaskStateMachine.MAX_RETRIES));
        }
    }

    @Nested
    @DisplayName("终态不应该有后续转换")
    class TerminalStates {

        @Test
        @DisplayName("COMPLETED 无后续状态")
        void shouldHaveNoNextFromCompleted() {
            assertTrue(sm.allowedNext(TaskStatus.COMPLETED).isEmpty());
        }

        @Test
        @DisplayName("FAILED 无后续状态")
        void shouldHaveNoNextFromFailed() {
            assertTrue(sm.allowedNext(TaskStatus.FAILED).isEmpty());
        }

        @Test
        @DisplayName("CANCELLED 无后续状态")
        void shouldHaveNoNextFromCancelled() {
            assertTrue(sm.allowedNext(TaskStatus.CANCELLED).isEmpty());
        }
    }

    @Nested
    @DisplayName("取消转换")
    class CancelTransitions {

        @Test
        @DisplayName("所有运行态都允许取消")
        void shouldAllowCancelFromAllRunning() {
            assertTrue(sm.canTransition(TaskStatus.PARSE_RUNNING, TaskStatus.CANCELLED));
            assertTrue(sm.canTransition(TaskStatus.EXTRACT_RUNNING, TaskStatus.CANCELLED));
            assertTrue(sm.canTransition(TaskStatus.CHECK_RUNNING, TaskStatus.CANCELLED));
            assertTrue(sm.canTransition(TaskStatus.REPORT_RUNNING, TaskStatus.CANCELLED));
        }

        @Test
        @DisplayName("终态不允许取消")
        void shouldNotCancelTerminal() {
            assertFalse(sm.canTransition(TaskStatus.COMPLETED, TaskStatus.CANCELLED));
            assertFalse(sm.canTransition(TaskStatus.FAILED, TaskStatus.CANCELLED));
            assertFalse(sm.canTransition(TaskStatus.CANCELLED, TaskStatus.CANCELLED));
        }
    }

    @Nested
    @DisplayName("辅助方法")
    class HelperMethods {

        @Test
        @DisplayName("isValid 应识别合法状态")
        void shouldRecognizeValidStatus() {
            assertTrue(sm.isValid("PENDING"));
            assertTrue(sm.isValid("COMPLETED"));
            assertTrue(sm.isValid("EXTRACT_PARTIAL"));
        }

        @Test
        @DisplayName("isValid 应拒绝非法状态")
        void shouldRejectInvalidStatus() {
            assertFalse(sm.isValid("INVALID"));
            assertFalse(sm.isValid(""));
            assertFalse(sm.isValid(null));
        }

        @Test
        @DisplayName("isTerminal 应正确判断终态")
        void shouldIdentifyTerminal() {
            assertTrue(TaskStatus.COMPLETED.isTerminal());
            assertTrue(TaskStatus.FAILED.isTerminal());
            assertTrue(TaskStatus.CANCELLED.isTerminal());
            assertFalse(TaskStatus.PENDING.isTerminal());
            assertFalse(TaskStatus.PARSE_RUNNING.isTerminal());
        }

        @Test
        @DisplayName("onStepSuccess 应返回正确的任务级别状态")
        void shouldReturnCorrectSuccessStatus() {
            assertEquals(TaskStatus.PARSE_SUCCESS, sm.onStepSuccess("PARSE"));
            assertEquals(TaskStatus.CHECK_SUCCESS, sm.onStepSuccess("CHECK"));
            assertEquals(TaskStatus.REPORT_SUCCESS, sm.onStepSuccess("REPORT"));
            assertEquals(TaskStatus.EXTRACT_SUCCESS, sm.onStepSuccess("EXTRACT_BS"));
            assertEquals(TaskStatus.EXTRACT_SUCCESS, sm.onStepSuccess("EXTRACT_IS"));
        }
    }

    @Nested
    @DisplayName("step status helpers")
    class StepStatusHelpers {

        @Test
        @DisplayName("should map every supported successful step")
        void shouldMapEverySupportedSuccessfulStep() {
            assertEquals(TaskStatus.PARSE_SUCCESS, sm.onStepSuccess("PARSE"));
            assertEquals(TaskStatus.EXTRACT_SUCCESS, sm.onStepSuccess("EXTRACT_BS"));
            assertEquals(TaskStatus.CHECK_SUCCESS, sm.onStepSuccess("CHECK"));
            assertEquals(TaskStatus.REPORT_SUCCESS, sm.onStepSuccess("REPORT"));
            assertEquals(TaskStatus.FAILED, sm.onStepSuccess("UNKNOWN"));
        }

        @Test
        @DisplayName("should map every supported failed step")
        void shouldMapEverySupportedFailedStep() {
            assertEquals(TaskStatus.PARSE_FAILED, sm.onStepFailure("PARSE"));
            assertEquals(TaskStatus.EXTRACT_FAILED, sm.onStepFailure("EXTRACT_CF"));
            assertEquals(TaskStatus.CHECK_FAILED, sm.onStepFailure("CHECK"));
            assertEquals(TaskStatus.REPORT_FAILED, sm.onStepFailure("REPORT"));
            assertEquals(TaskStatus.FAILED, sm.onStepFailure("UNKNOWN"));
        }

        @Test
        @DisplayName("should map running state and next stage for every route")
        void shouldMapRunningStateAndNextStageForEveryRoute() {
            assertEquals(TaskStatus.PARSE_RUNNING, sm.runningStateFor("PARSE"));
            assertEquals(TaskStatus.EXTRACT_RUNNING, sm.runningStateFor("EXTRACT_IS"));
            assertEquals(TaskStatus.CHECK_RUNNING, sm.runningStateFor("CHECK"));
            assertEquals(TaskStatus.REPORT_RUNNING, sm.runningStateFor("REPORT"));
            assertEquals(TaskStatus.PARSE_RUNNING, sm.runningStateFor("UNKNOWN"));

            assertEquals("EXTRACT_BS", sm.nextStepAfter("PARSE"));
            assertEquals("CHECK", sm.nextStepAfter("EXTRACT_BS"));
            assertEquals("CHECK", sm.nextStepAfter("EXTRACT_IS"));
            assertEquals("CHECK", sm.nextStepAfter("EXTRACT_CF"));
            assertEquals("CHECK", sm.nextStepAfter("EXTRACT_COMPLETE"));
            assertEquals("REPORT", sm.nextStepAfter("CHECK"));
            assertNull(sm.nextStepAfter("REPORT"));
            assertNull(sm.nextStepAfter("UNKNOWN"));
        }

        @Test
        @DisplayName("should choose every retry route and reject a non-failed state")
        void shouldChooseEveryRetryRouteAndRejectNonFailedState() {
            assertEquals(TaskStatus.PARSE_RETRY, sm.decideRetryOrFail(TaskStatus.PARSE_FAILED, 2));
            assertEquals(TaskStatus.EXTRACT_RETRY, sm.decideRetryOrFail(TaskStatus.EXTRACT_FAILED, 2));
            assertEquals(TaskStatus.CHECK_RETRY, sm.decideRetryOrFail(TaskStatus.CHECK_FAILED, 2));
            assertEquals(TaskStatus.REPORT_RETRY, sm.decideRetryOrFail(TaskStatus.REPORT_FAILED, 2));
            assertEquals(TaskStatus.FAILED, sm.decideRetryOrFail(TaskStatus.PARSE_RUNNING, 2));
        }
    }
}
