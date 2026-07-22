package com.finreport.service.reasoner;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.math.BigDecimal;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.transaction.reactive.TransactionalOperator;

import com.finreport.domain.entity.AccountingCheck;
import com.finreport.domain.entity.AnomalyRecord;
import com.finreport.domain.entity.Report;
import com.finreport.domain.entity.Task;
import com.finreport.repository.AccountingCheckRepository;
import com.finreport.repository.AnomalyRepository;
import com.finreport.repository.ReportRepository;
import com.finreport.repository.TaskRepository;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * CheckResultWriter 单元测试 — M3.04。
 *
 * <p>覆盖：M3.04 契约 payload 解析 / reportId 解析 / 批量持久化（事务包裹）/
 * 各种容错路径（空 result / rules 缺失 / rule_type 缺失 / anomalies 缺失 /
 * anomaly_type 白名单 / reportId 解析失败 / check 写库失败 / anomaly 写库失败）。</p>
 */
@ExtendWith(MockitoExtension.class)
@DisplayName("CheckResultWriter")
class CheckResultWriterTest {

    @Mock
    private TaskRepository taskRepo;

    @Mock
    private ReportRepository reportRepo;

    @Mock
    private AccountingCheckRepository checkRepo;

    @Mock
    private AnomalyRepository anomalyRepo;

    @Mock
    private TransactionalOperator transactionalOperator;

    private CheckResultWriter writer;

    @BeforeEach
    void setUp() {
        writer = new CheckResultWriter(taskRepo, reportRepo, checkRepo, anomalyRepo, transactionalOperator);
        // 事务包裹默认透传：直接执行传入的 Mono，不做真实事务边界（单元测试不需要真连接）。
        lenient().doAnswer(invocation -> invocation.getArgument(0))
                .when(transactionalOperator).transactional(any(Mono.class));
    }

    // ========================================================================
    // Payload builders
    // ========================================================================

    private static Map<String, Object> rulePayload(
            String ruleType, String ruleName, boolean isPass, String severity) {
        Map<String, Object> rule = new LinkedHashMap<>();
        rule.put("rule_type", ruleType);
        rule.put("rule_name", ruleName);
        rule.put("expected", "1000.00");
        rule.put("actual", "1000.00");
        rule.put("diff", "0.00");
        rule.put("is_pass", isPass);
        rule.put("severity", severity);
        rule.put("tolerance", "0.01");
        rule.put("note", "");
        rule.put("missing_items", List.of());
        rule.put("llm_reviewed", false);
        return rule;
    }

    private static Map<String, Object> anomalyPayload(
            String itemName, String anomalyType, String severity, String description) {
        Map<String, Object> anomaly = new LinkedHashMap<>();
        anomaly.put("item_name", itemName);
        anomaly.put("anomaly_type", anomalyType);
        anomaly.put("metric_value", "1.0");
        anomaly.put("threshold", "0.30");
        anomaly.put("description", description);
        anomaly.put("severity", severity);
        return anomaly;
    }

    private static Map<String, Object> fullPayload(
            List<Map<String, Object>> rules, List<Map<String, Object>> anomalies) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("rules", rules);
        payload.put("anomalies", anomalies);
        payload.put("confidence", 0.85);
        payload.put("report_period", "2024-12-31");
        return payload;
    }

    private void stubTaskWithRefReportId(String taskId, Long reportId) {
        Task task = Task.builder().id(taskId).refReportId(reportId).build();
        lenient().when(taskRepo.findById(taskId)).thenReturn(Mono.just(task));
    }

    private void stubSaveAllReturningIdentity() {
        when(checkRepo.saveAll(anyList())).thenAnswer(invocation -> {
            @SuppressWarnings("unchecked")
            List<AccountingCheck> items = (List<AccountingCheck>) invocation.getArgument(0);
            for (int i = 0; i < items.size(); i++) {
                items.get(i).setId((long) i + 1);
            }
            return Flux.fromIterable(items);
        });
        when(anomalyRepo.saveAll(anyList())).thenAnswer(invocation -> {
            @SuppressWarnings("unchecked")
            List<AnomalyRecord> items = (List<AnomalyRecord>) invocation.getArgument(0);
            for (int i = 0; i < items.size(); i++) {
                items.get(i).setId((long) i + 1);
            }
            return Flux.fromIterable(items);
        });
    }

    // ========================================================================
    // parseCheckResult (static helper tests)
    // ========================================================================

    @Nested
    @DisplayName("parseCheckResult")
    class ParseCheckResult {

        @Test
        @DisplayName("should parse 3 rules + 2 anomalies from well-formed payload")
        void shouldParseThreeRulesAndTwoAnomalies() {
            Map<String, Object> payload = fullPayload(
                    List.of(
                            rulePayload("balance_sheet_identity", "资产=负债+所有者权益", true, "INFO"),
                            rulePayload("net_income_to_retained", "净利润→未分配利润变动", true, "INFO"),
                            rulePayload("cash_flow_vs_net_income", "经营现金流 vs 净利润", false, "WARN")),
                    List.of(
                            anomalyPayload("货币资金", "yoy_change", "ERROR", "同比变动增长 100%"),
                            anomalyPayload("应收账款", "logic_conflict", "ERROR", "应收激增但营收下滑")));

            CheckResultWriter.ParsedCheckResult parsed = CheckResultWriter.parseCheckResult(payload);

            assertNotNull(parsed);
            assertEquals(3, parsed.rules().size());
            assertEquals(2, parsed.anomalies().size());

            AccountingCheck first = parsed.rules().get(0);
            assertEquals("balance_sheet_identity", first.getRuleType());
            assertEquals("资产=负债+所有者权益", first.getRuleName());
            assertEquals(new BigDecimal("1000.00"), first.getExpected());
            assertEquals(new BigDecimal("1000.00"), first.getActual());
            assertEquals(new BigDecimal("0.00"), first.getDiff());
            assertTrue(first.getIsPass());
            assertEquals("INFO", first.getSeverity());

            AnomalyRecord anomaly = parsed.anomalies().get(0);
            assertEquals("货币资金", anomaly.getItemName());
            assertEquals("yoy_change", anomaly.getAnomalyType());
            assertEquals(new BigDecimal("1.0"), anomaly.getMetricValue());
            assertEquals(new BigDecimal("0.30"), anomaly.getThreshold());
            assertEquals("ERROR", anomaly.getSeverity());
        }

        @Test
        @DisplayName("should return null when rules field is missing")
        void shouldReturnNullWhenRulesFieldMissing() {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("anomalies", List.of());
            payload.put("confidence", 0.85);

            assertNull(CheckResultWriter.parseCheckResult(payload));
        }

        @Test
        @DisplayName("should return null when rules is not a List")
        void shouldReturnNullWhenRulesIsNotList() {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("rules", "not-a-list");

            assertNull(CheckResultWriter.parseCheckResult(payload));
        }

        @Test
        @DisplayName("should return null when rules list is empty")
        void shouldReturnNullWhenRulesListIsEmpty() {
            Map<String, Object> payload = fullPayload(List.of(), List.of());

            assertNull(CheckResultWriter.parseCheckResult(payload));
        }

        @Test
        @DisplayName("should treat missing anomalies as empty list")
        void shouldTreatMissingAnomaliesAsEmptyList() {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("rules", List.of(rulePayload("balance_sheet_identity", "rule1", true, "INFO")));
            // No "anomalies" key — M3.01 阶段 L3 还未集成 AnomalyDetector 时合法。

            CheckResultWriter.ParsedCheckResult parsed = CheckResultWriter.parseCheckResult(payload);

            assertNotNull(parsed);
            assertEquals(1, parsed.rules().size());
            assertTrue(parsed.anomalies().isEmpty());
        }

        @Test
        @DisplayName("should skip rule entry when rule_type is missing")
        void shouldSkipRuleEntryWhenRuleTypeMissing() {
            Map<String, Object> badRule = new LinkedHashMap<>();
            badRule.put("rule_name", "no-type");
            badRule.put("is_pass", true);

            Map<String, Object> payload = fullPayload(
                    List.of(
                            badRule,
                            rulePayload("balance_sheet_identity", "valid-rule", true, "INFO")),
                    List.of());

            CheckResultWriter.ParsedCheckResult parsed = CheckResultWriter.parseCheckResult(payload);

            assertNotNull(parsed);
            assertEquals(1, parsed.rules().size());
            assertEquals("balance_sheet_identity", parsed.rules().get(0).getRuleType());
        }

        @Test
        @DisplayName("should skip anomaly entry when anomaly_type is missing")
        void shouldSkipAnomalyEntryWhenAnomalyTypeMissing() {
            Map<String, Object> badAnomaly = new LinkedHashMap<>();
            badAnomaly.put("item_name", "no-type");
            // No anomaly_type key

            Map<String, Object> payload = fullPayload(
                    List.of(rulePayload("balance_sheet_identity", "rule1", true, "INFO")),
                    List.of(badAnomaly));

            CheckResultWriter.ParsedCheckResult parsed = CheckResultWriter.parseCheckResult(payload);

            assertNotNull(parsed);
            assertTrue(parsed.anomalies().isEmpty());
        }

        @Test
        @DisplayName("should skip anomaly entry when anomaly_type is not in whitelist")
        void shouldSkipAnomalyEntryWhenAnomalyTypeNotInWhitelist() {
            Map<String, Object> payload = fullPayload(
                    List.of(rulePayload("balance_sheet_identity", "rule1", true, "INFO")),
                    List.of(anomalyPayload("test", "invalid_type", "ERROR", "unknown type")));

            CheckResultWriter.ParsedCheckResult parsed = CheckResultWriter.parseCheckResult(payload);

            assertNotNull(parsed);
            assertTrue(parsed.anomalies().isEmpty());
        }

        @Test
        @DisplayName("should accept all three valid anomaly types")
        void shouldAcceptAllThreeValidAnomalyTypes() {
            Map<String, Object> payload = fullPayload(
                    List.of(rulePayload("balance_sheet_identity", "rule1", true, "INFO")),
                    List.of(
                            anomalyPayload("a", "yoy_change", "WARN", "yoy"),
                            anomalyPayload("b", "qoq_change", "WARN", "qoq"),
                            anomalyPayload("c", "logic_conflict", "ERROR", "logic")));

            CheckResultWriter.ParsedCheckResult parsed = CheckResultWriter.parseCheckResult(payload);

            assertNotNull(parsed);
            assertEquals(3, parsed.anomalies().size());
        }

        @Test
        @DisplayName("should parse Decimal from number (not just string)")
        void shouldParseDecimalFromNumber() {
            Map<String, Object> rule = new LinkedHashMap<>();
            rule.put("rule_type", "balance_sheet_identity");
            rule.put("rule_name", "rule1");
            rule.put("expected", 1234.56);  // Number, not String
            rule.put("actual", 1234.56);
            rule.put("diff", 0.0);
            rule.put("is_pass", true);
            rule.put("severity", "INFO");

            Map<String, Object> payload = fullPayload(List.of(rule), List.of());

            CheckResultWriter.ParsedCheckResult parsed = CheckResultWriter.parseCheckResult(payload);

            assertNotNull(parsed);
            // Number.toString() → "1234.56"，BigDecimal 解析成功
            assertEquals(new BigDecimal("1234.56"), parsed.rules().get(0).getExpected());
        }

        @Test
        @DisplayName("should default severity to INFO when missing")
        void shouldDefaultSeverityToInfoWhenMissing() {
            Map<String, Object> rule = new LinkedHashMap<>();
            rule.put("rule_type", "balance_sheet_identity");
            rule.put("rule_name", "rule1");
            rule.put("is_pass", true);
            // No severity — defaults to INFO

            Map<String, Object> payload = fullPayload(List.of(rule), List.of());

            CheckResultWriter.ParsedCheckResult parsed = CheckResultWriter.parseCheckResult(payload);

            assertNotNull(parsed);
            assertEquals("INFO", parsed.rules().get(0).getSeverity());
        }

        @Test
        @DisplayName("should default anomaly severity to WARN when missing")
        void shouldDefaultAnomalySeverityToWarnWhenMissing() {
            Map<String, Object> anomaly = new LinkedHashMap<>();
            anomaly.put("item_name", "test");
            anomaly.put("anomaly_type", "yoy_change");
            // No severity — defaults to WARN

            Map<String, Object> payload = fullPayload(
                    List.of(rulePayload("balance_sheet_identity", "rule1", true, "INFO")),
                    List.of(anomaly));

            CheckResultWriter.ParsedCheckResult parsed = CheckResultWriter.parseCheckResult(payload);

            assertNotNull(parsed);
            assertEquals("WARN", parsed.anomalies().get(0).getSeverity());
        }
    }

    // ========================================================================
    // writeCheckResult
    // ========================================================================

    @Nested
    @DisplayName("writeCheckResult")
    class WriteCheckResult {

        @Test
        @DisplayName("should persist 3 rules + 2 anomalies when payload is well-formed")
        void shouldPersistRulesAndAnomaliesWhenPayloadIsWellFormed() {
            String taskId = "task-abc";
            Long reportId = 100L;
            stubTaskWithRefReportId(taskId, reportId);
            stubSaveAllReturningIdentity();

            Map<String, Object> payload = fullPayload(
                    List.of(
                            rulePayload("balance_sheet_identity", "资产=负债+所有者权益", true, "INFO"),
                            rulePayload("net_income_to_retained", "净利润→未分配利润变动", true, "INFO"),
                            rulePayload("cash_flow_vs_net_income", "经营现金流 vs 净利润", false, "WARN")),
                    List.of(
                            anomalyPayload("货币资金", "yoy_change", "ERROR", "同比变动增长 100%"),
                            anomalyPayload("应收账款", "logic_conflict", "ERROR", "应收激增但营收下滑")));

            StepVerifier.create(writer.writeCheckResult(taskId, payload))
                    .assertNext(count -> assertEquals(5, count))
                    .verifyComplete();

            @SuppressWarnings("unchecked")
            ArgumentCaptor<List<AccountingCheck>> checkCaptor =
                    ArgumentCaptor.forClass(List.class);
            verify(checkRepo).saveAll(checkCaptor.capture());
            List<AccountingCheck> savedChecks = checkCaptor.getValue();
            assertEquals(3, savedChecks.size());
            assertEquals(reportId, savedChecks.get(0).getReportId());
            assertEquals("balance_sheet_identity", savedChecks.get(0).getRuleType());
            assertEquals("资产=负债+所有者权益", savedChecks.get(0).getRuleName());

            @SuppressWarnings("unchecked")
            ArgumentCaptor<List<AnomalyRecord>> anomalyCaptor =
                    ArgumentCaptor.forClass(List.class);
            verify(anomalyRepo).saveAll(anomalyCaptor.capture());
            List<AnomalyRecord> savedAnomalies = anomalyCaptor.getValue();
            assertEquals(2, savedAnomalies.size());
            assertEquals(reportId, savedAnomalies.get(0).getReportId());
            assertEquals("货币资金", savedAnomalies.get(0).getItemName());

            // 事务包裹被调用一次（spec §8.4 任务边界 = 事务边界）。
            verify(transactionalOperator).transactional(any(Mono.class));
        }

        @Test
        @DisplayName("should persist rules only when anomalies list is empty")
        void shouldPersistRulesOnlyWhenAnomaliesListIsEmpty() {
            String taskId = "task-no-anomaly";
            Long reportId = 200L;
            stubTaskWithRefReportId(taskId, reportId);
            stubSaveAllReturningIdentity();

            Map<String, Object> payload = fullPayload(
                    List.of(rulePayload("balance_sheet_identity", "rule1", true, "INFO")),
                    List.of());

            StepVerifier.create(writer.writeCheckResult(taskId, payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();

            verify(checkRepo).saveAll(anyList());
            verify(anomalyRepo).saveAll(anyList());
        }

        @Test
        @DisplayName("should resolve reportId via reportRepo when task.refReportId is null")
        void shouldResolveReportIdViaReportRepoWhenRefReportIdIsNull() {
            String taskId = "task-fallback";
            Long reportId = 999L;
            Task task = Task.builder().id(taskId).refReportId(null).build();
            when(taskRepo.findById(taskId)).thenReturn(Mono.just(task));
            Report report = Report.builder().id(reportId).taskId(taskId).build();
            when(reportRepo.findByTaskId(taskId)).thenReturn(Mono.just(report));
            stubSaveAllReturningIdentity();

            Map<String, Object> payload = fullPayload(
                    List.of(rulePayload("balance_sheet_identity", "rule1", true, "INFO")),
                    List.of());

            StepVerifier.create(writer.writeCheckResult(taskId, payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();

            verify(reportRepo).findByTaskId(taskId);
        }

        @Test
        @DisplayName("should return 0 when result is null")
        void shouldReturnZeroWhenResultIsNull() {
            StepVerifier.create(writer.writeCheckResult("task-x", null))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return 0 when result is empty")
        void shouldReturnZeroWhenResultIsEmpty() {
            StepVerifier.create(writer.writeCheckResult("task-x", Map.of()))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return 0 when rules field is missing")
        void shouldReturnZeroWhenRulesFieldIsMissing() {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("anomalies", List.of());
            payload.put("confidence", 0.85);

            StepVerifier.create(writer.writeCheckResult("task-x", payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();

            verify(checkRepo, never()).saveAll(anyList());
        }

        @Test
        @DisplayName("should return 0 when task does not exist")
        void shouldReturnZeroWhenTaskDoesNotExist() {
            when(taskRepo.findById("task-missing")).thenReturn(Mono.empty());

            Map<String, Object> payload = fullPayload(
                    List.of(rulePayload("balance_sheet_identity", "rule1", true, "INFO")),
                    List.of());

            StepVerifier.create(writer.writeCheckResult("task-missing", payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();

            verify(checkRepo, never()).saveAll(anyList());
        }

        @Test
        @DisplayName("should return 0 when checkRepo.saveAll fails")
        void shouldReturnZeroWhenCheckRepoSaveAllFails() {
            String taskId = "task-fail";
            Long reportId = 300L;
            stubTaskWithRefReportId(taskId, reportId);
            when(checkRepo.saveAll(anyList())).thenReturn(Flux.error(new RuntimeException("DB down")));
            when(anomalyRepo.saveAll(anyList())).thenReturn(Flux.empty());

            Map<String, Object> payload = fullPayload(
                    List.of(rulePayload("balance_sheet_identity", "rule1", true, "INFO")),
                    List.of());

            // 写库失败不抛异常，返回 0（spec §8.4 失败不强制回滚）。
            StepVerifier.create(writer.writeCheckResult(taskId, payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return 0 when anomalyRepo.saveAll fails")
        void shouldReturnZeroWhenAnomalyRepoSaveAllFails() {
            String taskId = "task-anomaly-fail";
            Long reportId = 350L;
            stubTaskWithRefReportId(taskId, reportId);
            when(checkRepo.saveAll(anyList())).thenAnswer(invocation -> {
                @SuppressWarnings("unchecked")
                List<AccountingCheck> items = (List<AccountingCheck>) invocation.getArgument(0);
                return Flux.fromIterable(items);
            });
            when(anomalyRepo.saveAll(anyList())).thenReturn(Flux.error(new RuntimeException("DB down")));

            Map<String, Object> payload = fullPayload(
                    List.of(rulePayload("balance_sheet_identity", "rule1", true, "INFO")),
                    List.of(anomalyPayload("货币资金", "yoy_change", "ERROR", "desc")));

            // 写库失败不抛异常，返回 0（spec §8.4 失败不强制回滚）。
            StepVerifier.create(writer.writeCheckResult(taskId, payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should default isPass to null when missing")
        void shouldDefaultIsPassToNullWhenMissing() {
            String taskId = "task-no-ispass";
            Long reportId = 400L;
            stubTaskWithRefReportId(taskId, reportId);
            stubSaveAllReturningIdentity();

            Map<String, Object> rule = new LinkedHashMap<>();
            rule.put("rule_type", "balance_sheet_identity");
            rule.put("rule_name", "rule1");
            // No is_pass — defaults to null
            rule.put("severity", "INFO");

            Map<String, Object> payload = fullPayload(List.of(rule), List.of());

            StepVerifier.create(writer.writeCheckResult(taskId, payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();

            @SuppressWarnings("unchecked")
            ArgumentCaptor<List<AccountingCheck>> captor = ArgumentCaptor.forClass(List.class);
            verify(checkRepo).saveAll(captor.capture());
            assertNull(captor.getValue().get(0).getIsPass());
        }

        @Test
        @DisplayName("should not call saveAll when transactionalOperator is invoked but skipped")
        void shouldWrapPersistInTransaction() {
            String taskId = "task-tx";
            Long reportId = 450L;
            stubTaskWithRefReportId(taskId, reportId);
            stubSaveAllReturningIdentity();
            // 模拟事务包装直接透传（setUp 默认行为已在 @BeforeEach 配置）。

            Map<String, Object> payload = fullPayload(
                    List.of(rulePayload("balance_sheet_identity", "rule1", true, "INFO")),
                    List.of());

            StepVerifier.create(writer.writeCheckResult(taskId, payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();

            // 验证 persistAll 链被 transactionalOperator 包裹。
            verify(transactionalOperator).transactional(any(Mono.class));
        }
    }
}
