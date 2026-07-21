package com.finreport.service.statement;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.when;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.HttpStatus;

import com.finreport.domain.entity.FinancialStatementItem;
import com.finreport.domain.entity.Report;
import com.finreport.exception.BusinessException;
import com.finreport.repository.FinancialStatementRepository;
import com.finreport.repository.ReportRepository;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * StatementQueryService 单测 — M2.11。
 *
 * <p>覆盖：归属校验（不存在/他人/正常）+ 三表分组 + 元数据映射。</p>
 */
@ExtendWith(MockitoExtension.class)
@DisplayName("StatementQueryService")
class StatementQueryServiceTest {

    @Mock
    private ReportRepository reportRepo;

    @Mock
    private FinancialStatementRepository fsRepo;

    private StatementQueryService service() {
        return new StatementQueryService(reportRepo, fsRepo);
    }

    private static Report report(Long id, Long userId) {
        Report r = new Report();
        r.setId(id);
        r.setUserId(userId);
        r.setTaskId("task-" + id);
        r.setCompanyCode("600519");
        r.setCompanyName("贵州茅台");
        r.setReportType("ANNUAL");
        r.setReportPeriod("2024-12-31");
        r.setPageCount(120);
        r.setParseStatus("COMPLETED");
        r.setPdfObjectKey("reports/" + id + ".pdf");
        r.setCreatedAt(LocalDateTime.parse("2026-07-21T10:00:00"));
        return r;
    }

    private static FinancialStatementItem item(Long id, String type, String name, BigDecimal value) {
        FinancialStatementItem item = new FinancialStatementItem();
        item.setId(id);
        item.setReportId(11L);
        item.setStatementType(type);
        item.setItemName(name);
        item.setItemValue(value);
        item.setCurrency("CNY");
        item.setUnit("元");
        item.setScope("合并");
        item.setPeriodType("本期");
        item.setConfidence(new BigDecimal("0.95"));
        item.setSourcePage(5);
        return item;
    }

    @Nested
    @DisplayName("getReportDetail")
    class GetReportDetail {

        @Test
        @DisplayName("should return detail when report belongs to user")
        void shouldReturnDetailWhenOwned() {
            when(reportRepo.findById(11L)).thenReturn(Mono.just(report(11L, 7L)));
            var detail = service().getReportDetail(11L, 7L).block();

            assertNotNull(detail);
            assertEquals(11L, detail.id());
            assertEquals("贵州茅台", detail.companyName());
            assertEquals("task-11", detail.taskId());
            assertEquals("ANNUAL", detail.reportType());
            assertEquals(Integer.valueOf(120), detail.pageCount());
        }

        @Test
        @DisplayName("should throw REPORT_NOT_FOUND when report does not exist")
        void shouldThrowWhenMissing() {
            when(reportRepo.findById(99L)).thenReturn(Mono.empty());

            StepVerifier.create(service().getReportDetail(99L, 7L))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && b.getStatus() == HttpStatus.NOT_FOUND
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }

        @Test
        @DisplayName("should throw REPORT_NOT_FOUND when report belongs to another user")
        void shouldThrowWhenOwnedByOtherUser() {
            when(reportRepo.findById(11L)).thenReturn(Mono.just(report(11L, 7L)));

            StepVerifier.create(service().getReportDetail(11L, 8L))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && b.getStatus() == HttpStatus.NOT_FOUND
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }
    }

    @Nested
    @DisplayName("getStatements")
    class GetStatements {

        @Test
        @DisplayName("should group items by statement_type when report belongs to user")
        void shouldGroupByStatementType() {
            when(reportRepo.findById(11L)).thenReturn(Mono.just(report(11L, 7L)));
            when(fsRepo.findByReportIdAndStatementTypeOrderByItemNameAsc(eq(11L), eq("balance_sheet")))
                    .thenReturn(Flux.just(
                            item(1L, "balance_sheet", "货币资金", new BigDecimal("1.5e9")),
                            item(2L, "balance_sheet", "应收账款", new BigDecimal("2.3e8"))));
            when(fsRepo.findByReportIdAndStatementTypeOrderByItemNameAsc(eq(11L), eq("income_statement")))
                    .thenReturn(Flux.just(item(3L, "income_statement", "营业收入", new BigDecimal("8.8e9"))));
            when(fsRepo.findByReportIdAndStatementTypeOrderByItemNameAsc(eq(11L), eq("cash_flow")))
                    .thenReturn(Flux.just(item(4L, "cash_flow", "经营活动现金流净额", new BigDecimal("1.5e9"))));

            var resp = service().getStatements(11L, 7L).block();

            assertNotNull(resp);
            assertEquals(2, resp.balanceSheet().size());
            assertEquals(1, resp.incomeStatement().size());
            assertEquals(1, resp.cashFlow().size());
            assertEquals("货币资金", resp.balanceSheet().get(0).itemName());
            assertEquals("营业收入", resp.incomeStatement().get(0).itemName());
            assertEquals("经营活动现金流净额", resp.cashFlow().get(0).itemName());
            assertEquals("CNY", resp.balanceSheet().get(0).currency());
            assertEquals("合并", resp.balanceSheet().get(0).scope());
            assertEquals(Integer.valueOf(5), resp.balanceSheet().get(0).sourcePage());
        }

        @Test
        @DisplayName("should return empty lists when report has no statements")
        void shouldReturnEmptyListsWhenNoStatements() {
            when(reportRepo.findById(11L)).thenReturn(Mono.just(report(11L, 7L)));
            when(fsRepo.findByReportIdAndStatementTypeOrderByItemNameAsc(eq(11L), eq("balance_sheet")))
                    .thenReturn(Flux.empty());
            when(fsRepo.findByReportIdAndStatementTypeOrderByItemNameAsc(eq(11L), eq("income_statement")))
                    .thenReturn(Flux.empty());
            when(fsRepo.findByReportIdAndStatementTypeOrderByItemNameAsc(eq(11L), eq("cash_flow")))
                    .thenReturn(Flux.empty());

            var resp = service().getStatements(11L, 7L).block();

            assertNotNull(resp);
            assertEquals(List.of(), resp.balanceSheet());
            assertEquals(List.of(), resp.incomeStatement());
            assertEquals(List.of(), resp.cashFlow());
        }

        @Test
        @DisplayName("should throw REPORT_NOT_FOUND when report does not exist")
        void shouldThrowWhenReportMissing() {
            when(reportRepo.findById(99L)).thenReturn(Mono.empty());

            StepVerifier.create(service().getStatements(99L, 7L))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }

        @Test
        @DisplayName("should throw REPORT_NOT_FOUND when report belongs to another user")
        void shouldThrowWhenOwnedByOtherUser() {
            when(reportRepo.findById(11L)).thenReturn(Mono.just(report(11L, 7L)));

            StepVerifier.create(service().getStatements(11L, 8L))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }
    }
}
