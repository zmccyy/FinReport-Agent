package com.finreport.service.statement;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyString;
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

import com.finreport.domain.entity.FinancialStatementItem;
import com.finreport.domain.entity.Report;
import com.finreport.domain.entity.Task;
import com.finreport.repository.FinancialStatementRepository;
import com.finreport.repository.ReportRepository;
import com.finreport.repository.TaskRepository;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * StatementWriter 单元测试 — M2.09。
 *
 * <p>覆盖：M2.09 契约 payload 解析 / reportId 解析 / 批量持久化 /
 * 各种容错路径（空 result / success=false / 非 EXTRACT step / statement 缺失 /
 * items 为空 / reportId 解析失败 / 写库异常）。</p>
 */
@ExtendWith(MockitoExtension.class)
@DisplayName("StatementWriter")
class StatementWriterTest {

    @Mock
    private TaskRepository taskRepo;

    @Mock
    private ReportRepository reportRepo;

    @Mock
    private FinancialStatementRepository fsRepo;

    private StatementWriter writer;

    @BeforeEach
    void setUp() {
        writer = new StatementWriter(taskRepo, reportRepo, fsRepo);
    }

    private static Map<String, Object> payload(String statementType, List<Map<String, Object>> items) {
        Map<String, Object> statement = new LinkedHashMap<>();
        statement.put("report_period", "2024-12-31");
        statement.put("currency", "CNY");
        statement.put("unit", "元");
        statement.put("statements", Map.of(statementType, items));

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("success", true);
        payload.put("statement", statement);
        payload.put("validation", Map.of("is_valid", true, "issues", List.of(), "error_hint", ""));
        payload.put("confidence", 0.92);
        payload.put("source_page", 5);
        return payload;
    }

    private static Map<String, Object> item(String name, double value) {
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("item", name);
        item.put("value", value);
        item.put("scope", "合并");
        item.put("period", "本期");
        return item;
    }

    private void stubTaskWithRefReportId(String taskId, Long reportId) {
        Task task = Task.builder().id(taskId).refReportId(reportId).build();
        lenient().when(taskRepo.findById(taskId)).thenReturn(Mono.just(task));
    }

    private void stubTaskWithoutRefReportId(String taskId, Long reportId) {
        Task task = Task.builder().id(taskId).refReportId(null).build();
        when(taskRepo.findById(taskId)).thenReturn(Mono.just(task));
        Report report = Report.builder().id(reportId).taskId(taskId).build();
        when(reportRepo.findByTaskId(taskId)).thenReturn(Mono.just(report));
    }

    private void stubSaveAllReturningIdentity() {
        when(fsRepo.saveAll(anyList())).thenAnswer(invocation -> {
            @SuppressWarnings("unchecked")
            List<FinancialStatementItem> items = (List<FinancialStatementItem>) invocation.getArgument(0);
            // 模拟 R2DBC 回填 id
            for (int i = 0; i < items.size(); i++) {
                items.get(i).setId((long) i + 1);
            }
            return Flux.fromIterable(items);
        });
    }

    @Nested
    @DisplayName("writeStatement")
    class WriteStatement {

        @Test
        @DisplayName("should persist BS items when payload is well-formed")
        void shouldPersistBsItemsWhenPayloadIsWellFormed() {
            String taskId = "task-abc";
            Long reportId = 100L;
            stubTaskWithRefReportId(taskId, reportId);
            stubSaveAllReturningIdentity();

            Map<String, Object> payload = payload("balance_sheet", List.of(
                    item("货币资金", 1.23e9),
                    item("资产总计", 5.67e10)));

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(2, count))
                    .verifyComplete();

            @SuppressWarnings("unchecked")
            ArgumentCaptor<List<FinancialStatementItem>> captor =
                    ArgumentCaptor.forClass(List.class);
            verify(fsRepo).saveAll(captor.capture());
            List<FinancialStatementItem> saved = captor.getValue();
            assertEquals(2, saved.size());
            assertEquals(reportId, saved.get(0).getReportId());
            assertEquals("balance_sheet", saved.get(0).getStatementType());
            assertEquals("货币资金", saved.get(0).getItemName());
            assertEquals(new BigDecimal("1.23E9"), saved.get(0).getItemValue());
            assertEquals("CNY", saved.get(0).getCurrency());
            assertEquals("元", saved.get(0).getUnit());
            assertEquals("合并", saved.get(0).getScope());
            assertEquals("本期", saved.get(0).getPeriodType());
            assertEquals(new BigDecimal("0.92"), saved.get(0).getConfidence());
            assertEquals(5, saved.get(0).getSourcePage());
        }

        @Test
        @DisplayName("should map IS step to income_statement type")
        void shouldMapIsStepToIncomeStatementType() {
            String taskId = "task-is";
            Long reportId = 200L;
            stubTaskWithRefReportId(taskId, reportId);
            stubSaveAllReturningIdentity();

            Map<String, Object> payload = payload("income_statement",
                    List.of(item("营业收入", 8.9e9)));

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_IS", payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();

            @SuppressWarnings("unchecked")
            ArgumentCaptor<List<FinancialStatementItem>> captor =
                    ArgumentCaptor.forClass(List.class);
            verify(fsRepo).saveAll(captor.capture());
            assertEquals("income_statement", captor.getValue().get(0).getStatementType());
        }

        @Test
        @DisplayName("should map CF step to cash_flow type")
        void shouldMapCfStepToCashFlowType() {
            String taskId = "task-cf";
            Long reportId = 300L;
            stubTaskWithRefReportId(taskId, reportId);
            stubSaveAllReturningIdentity();

            Map<String, Object> payload = payload("cash_flow",
                    List.of(item("经营活动产生的现金流量净额", 1.2e9)));

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_CF", payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();

            @SuppressWarnings("unchecked")
            ArgumentCaptor<List<FinancialStatementItem>> captor =
                    ArgumentCaptor.forClass(List.class);
            verify(fsRepo).saveAll(captor.capture());
            assertEquals("cash_flow", captor.getValue().get(0).getStatementType());
        }

        @Test
        @DisplayName("should resolve reportId via reportRepo when task.refReportId is null")
        void shouldResolveReportIdViaReportRepoWhenRefReportIdIsNull() {
            String taskId = "task-fallback";
            Long reportId = 999L;
            stubTaskWithoutRefReportId(taskId, reportId);
            stubSaveAllReturningIdentity();

            Map<String, Object> payload = payload("balance_sheet",
                    List.of(item("货币资金", 1.0)));

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();

            verify(reportRepo).findByTaskId(taskId);
        }

        @Test
        @DisplayName("should return 0 when result is null")
        void shouldReturnZeroWhenResultIsNull() {
            StepVerifier.create(writer.writeStatement("task-x", "EXTRACT_BS", null))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
            verify(fsRepo, never()).saveAll(anyList());
        }

        @Test
        @DisplayName("should return 0 when result is empty")
        void shouldReturnZeroWhenResultIsEmpty() {
            StepVerifier.create(writer.writeStatement("task-x", "EXTRACT_BS", Map.of()))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
            verify(fsRepo, never()).saveAll(anyList());
        }

        @Test
        @DisplayName("should return 0 when result.success is false")
        void shouldReturnZeroWhenResultSuccessIsFalse() {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("success", false);
            payload.put("statement", Map.of());

            StepVerifier.create(writer.writeStatement("task-x", "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
            verify(fsRepo, never()).saveAll(anyList());
        }

        @Test
        @DisplayName("should return 0 when step is not an EXTRACT step")
        void shouldReturnZeroWhenStepIsNotExtract() {
            Map<String, Object> payload = payload("balance_sheet",
                    List.of(item("货币资金", 1.0)));

            StepVerifier.create(writer.writeStatement("task-x", "PARSE", payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
            verify(fsRepo, never()).saveAll(anyList());
        }

        @Test
        @DisplayName("should return 0 when statement field is missing")
        void shouldReturnZeroWhenStatementFieldIsMissing() {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("success", true);
            // no statement field

            StepVerifier.create(writer.writeStatement("task-x", "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
            verify(fsRepo, never()).saveAll(anyList());
        }

        @Test
        @DisplayName("should return 0 when statements dict is missing")
        void shouldReturnZeroWhenStatementsDictIsMissing() {
            Map<String, Object> statement = new LinkedHashMap<>();
            statement.put("report_period", "2024-12-31");
            // no statements field

            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("success", true);
            payload.put("statement", statement);

            StepVerifier.create(writer.writeStatement("task-x", "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
            verify(fsRepo, never()).saveAll(anyList());
        }

        @Test
        @DisplayName("should return 0 when items list is empty")
        void shouldReturnZeroWhenItemsListIsEmpty() {
            Map<String, Object> payload = payload("balance_sheet", List.of());

            stubTaskWithRefReportId("task-x", 1L);

            StepVerifier.create(writer.writeStatement("task-x", "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
            verify(fsRepo, never()).saveAll(anyList());
        }

        @Test
        @DisplayName("should skip items without item name")
        void shouldSkipItemsWithoutItemName() {
            String taskId = "task-skip";
            stubTaskWithRefReportId(taskId, 1L);
            stubSaveAllReturningIdentity();

            Map<String, Object> badItem = new LinkedHashMap<>();
            badItem.put("item", "");
            badItem.put("value", 1.0);

            Map<String, Object> payload = payload("balance_sheet",
                    List.of(badItem, item("货币资金", 2.0)));

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should skip items without value")
        void shouldSkipItemsWithoutValue() {
            String taskId = "task-skip-value";
            stubTaskWithRefReportId(taskId, 1L);
            stubSaveAllReturningIdentity();

            Map<String, Object> badItem = new LinkedHashMap<>();
            badItem.put("item", "无值科目");
            // no value field

            Map<String, Object> payload = payload("balance_sheet",
                    List.of(badItem, item("货币资金", 2.0)));

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should parse value from numeric string with thousand separators")
        void shouldParseValueFromNumericStringWithThousandSeparators() {
            String taskId = "task-str";
            stubTaskWithRefReportId(taskId, 1L);
            stubSaveAllReturningIdentity();

            Map<String, Object> strItem = new LinkedHashMap<>();
            strItem.put("item", "应收账款");
            strItem.put("value", "1,234,567.89");
            strItem.put("scope", "合并");
            strItem.put("period", "本期");

            Map<String, Object> payload = payload("balance_sheet", List.of(strItem));

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();

            @SuppressWarnings("unchecked")
            ArgumentCaptor<List<FinancialStatementItem>> captor =
                    ArgumentCaptor.forClass(List.class);
            verify(fsRepo).saveAll(captor.capture());
            BigDecimal actual = captor.getValue().get(0).getItemValue();
            assertNotNull(actual);
            assertEquals(new BigDecimal("1234567.89"), actual);
        }

        @Test
        @DisplayName("should return 0 when reportId cannot be resolved")
        void shouldReturnZeroWhenReportIdCannotBeResolved() {
            String taskId = "task-no-report";
            Task task = Task.builder().id(taskId).refReportId(null).build();
            when(taskRepo.findById(taskId)).thenReturn(Mono.just(task));
            when(reportRepo.findByTaskId(taskId)).thenReturn(Mono.empty());

            Map<String, Object> payload = payload("balance_sheet",
                    List.of(item("货币资金", 1.0)));

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
            verify(fsRepo, never()).saveAll(anyList());
        }

        @Test
        @DisplayName("should return 0 when task is not found")
        void shouldReturnZeroWhenTaskIsNotFound() {
            when(taskRepo.findById(anyString())).thenReturn(Mono.empty());

            Map<String, Object> payload = payload("balance_sheet",
                    List.of(item("货币资金", 1.0)));

            StepVerifier.create(writer.writeStatement("task-missing", "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
            verify(fsRepo, never()).saveAll(anyList());
        }

        @Test
        @DisplayName("should return 0 and not throw when fsRepo.saveAll fails")
        void shouldReturnZeroAndNotThrowWhenFsRepoSaveAllFails() {
            String taskId = "task-fail";
            stubTaskWithRefReportId(taskId, 1L);
            when(fsRepo.saveAll(anyList())).thenReturn(Flux.error(new RuntimeException("db down")));

            Map<String, Object> payload = payload("balance_sheet",
                    List.of(item("货币资金", 1.0)));

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should default currency and unit when missing")
        void shouldDefaultCurrencyAndUnitWhenMissing() {
            String taskId = "task-defaults";
            stubTaskWithRefReportId(taskId, 1L);
            stubSaveAllReturningIdentity();

            Map<String, Object> statement = new LinkedHashMap<>();
            statement.put("report_period", "2024-12-31");
            // no currency / unit
            statement.put("statements", Map.of("balance_sheet", List.of(item("货币资金", 1.0))));

            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("success", true);
            payload.put("statement", statement);

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();

            @SuppressWarnings("unchecked")
            ArgumentCaptor<List<FinancialStatementItem>> captor =
                    ArgumentCaptor.forClass(List.class);
            verify(fsRepo).saveAll(captor.capture());
            FinancialStatementItem saved = captor.getValue().get(0);
            assertEquals("CNY", saved.getCurrency());
            assertEquals("元", saved.getUnit());
        }

        @Test
        @DisplayName("should default scope and period when missing on item")
        void shouldDefaultScopeAndPeriodWhenMissingOnItem() {
            String taskId = "task-defaults-item";
            stubTaskWithRefReportId(taskId, 1L);
            stubSaveAllReturningIdentity();

            Map<String, Object> bareItem = new LinkedHashMap<>();
            bareItem.put("item", "货币资金");
            bareItem.put("value", 1.0);
            // no scope / period

            Map<String, Object> payload = payload("balance_sheet", List.of(bareItem));

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(1, count))
                    .verifyComplete();

            @SuppressWarnings("unchecked")
            ArgumentCaptor<List<FinancialStatementItem>> captor =
                    ArgumentCaptor.forClass(List.class);
            verify(fsRepo).saveAll(captor.capture());
            FinancialStatementItem saved = captor.getValue().get(0);
            assertEquals("合并", saved.getScope());
            assertEquals("本期", saved.getPeriodType());
        }

        @Test
        @DisplayName("should not call saveAll when items list contains only non-map entries")
        void shouldNotCallSaveAllWhenItemsListContainsOnlyNonMapEntries() {
            String taskId = "task-non-map";
            stubTaskWithRefReportId(taskId, 1L);

            Map<String, Object> statement = new LinkedHashMap<>();
            statement.put("report_period", "2024-12-31");
            statement.put("statements", Map.of("balance_sheet", List.of("not-a-map", 42)));

            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("success", true);
            payload.put("statement", statement);

            StepVerifier.create(writer.writeStatement(taskId, "EXTRACT_BS", payload))
                    .assertNext(count -> assertEquals(0, count))
                    .verifyComplete();
            verify(fsRepo, never()).saveAll(anyList());
        }
    }
}
