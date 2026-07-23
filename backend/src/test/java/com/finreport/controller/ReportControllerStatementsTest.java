package com.finreport.controller;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.when;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.HttpStatus;

import com.finreport.domain.dto.ReportDetailResponse;
import com.finreport.domain.dto.StatementItemResponse;
import com.finreport.domain.dto.StatementsResponse;
import com.finreport.exception.BusinessException;
import com.finreport.service.statement.StatementQueryService;

import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * ReportController 三表 / 详情端点单测 — M2.11。
 *
 * <p>验证 controller 薄壳：仅做参数透传 + HTTP 状态码映射，
 * 业务逻辑（归属校验、分组）由 {@link StatementQueryService} 完成。</p>
 */
@ExtendWith(MockitoExtension.class)
@DisplayName("ReportController statements/detail endpoints")
class ReportControllerStatementsTest {

    @Mock
    private StatementQueryService statementQueryService;

    @Nested
    @DisplayName("GET /reports/{reportId}")
    class GetReport {

        @Test
        @DisplayName("should return 200 with report detail when report exists and belongs to user")
        void shouldReturnReportDetailWhenOwned() {
            ReportController controller = new ReportController(null, statementQueryService, null, null, null);
            ReportDetailResponse detail = new ReportDetailResponse(
                    11L, "task-1", "600519", "贵州茅台", "ANNUAL", "2024-12-31",
                    120, "COMPLETED", "reports/11.pdf", Instant.parse("2026-07-21T10:00:00Z"));
            when(statementQueryService.getReportDetail(eq(11L), eq(7L))).thenReturn(Mono.just(detail));

            var resp = controller.getReport(11L, 7L).block();

            assertEquals(HttpStatus.OK, resp.getStatusCode());
            assertEquals(detail, resp.getBody());
        }

        @Test
        @DisplayName("should propagate REPORT_NOT_FOUND when report does not exist")
        void shouldPropagateNotFoundWhenReportMissing() {
            ReportController controller = new ReportController(null, statementQueryService, null, null, null);
            when(statementQueryService.getReportDetail(eq(99L), eq(7L)))
                    .thenReturn(Mono.error(new BusinessException(
                            org.springframework.http.HttpStatus.NOT_FOUND,
                            "REPORT_NOT_FOUND", "报告不存在: 99")));

            StepVerifier.create(controller.getReport(99L, 7L))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }

        @Test
        @DisplayName("should propagate REPORT_NOT_FOUND when report belongs to another user")
        void shouldPropagateNotFoundWhenReportOwnedByOtherUser() {
            ReportController controller = new ReportController(null, statementQueryService, null, null, null);
            when(statementQueryService.getReportDetail(eq(11L), eq(8L)))
                    .thenReturn(Mono.error(new BusinessException(
                            org.springframework.http.HttpStatus.NOT_FOUND,
                            "REPORT_NOT_FOUND", "报告不存在: 11")));

            StepVerifier.create(controller.getReport(11L, 8L))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }
    }

    @Nested
    @DisplayName("GET /reports/{reportId}/statements")
    class GetStatements {

        @Test
        @DisplayName("should return 200 with grouped BS/IS/CF lists when report owned")
        void shouldReturnGroupedStatementsWhenOwned() {
            ReportController controller = new ReportController(null, statementQueryService, null, null, null);
            StatementItemResponse bsItem = new StatementItemResponse(
                    1L, "balance_sheet", "货币资金", new BigDecimal("1587023498.50"),
                    "CNY", "元", "合并", "本期", new BigDecimal("0.95"), 5);
            StatementItemResponse isItem = new StatementItemResponse(
                    2L, "income_statement", "营业收入", new BigDecimal("8800000000.00"),
                    "CNY", "元", "合并", "本期", new BigDecimal("0.93"), 8);
            StatementItemResponse cfItem = new StatementItemResponse(
                    3L, "cash_flow", "经营活动产生的现金流量净额", new BigDecimal("1500000000.00"),
                    "CNY", "元", "合并", "本期", new BigDecimal("0.91"), 12);
            StatementsResponse resp = new StatementsResponse(
                    List.of(bsItem), List.of(isItem), List.of(cfItem));
            when(statementQueryService.getStatements(eq(11L), eq(7L))).thenReturn(Mono.just(resp));

            var result = controller.getStatements(11L, 7L).block();

            assertEquals(HttpStatus.OK, result.getStatusCode());
            StatementsResponse body = result.getBody();
            assertNotNull(body);
            assertEquals(1, body.balanceSheet().size());
            assertEquals(1, body.incomeStatement().size());
            assertEquals(1, body.cashFlow().size());
            assertEquals("货币资金", body.balanceSheet().get(0).itemName());
            assertEquals("营业收入", body.incomeStatement().get(0).itemName());
            assertEquals("经营活动产生的现金流量净额", body.cashFlow().get(0).itemName());
        }

        @Test
        @DisplayName("should return empty lists when report has no extracted statements yet")
        void shouldReturnEmptyListsWhenNoStatements() {
            ReportController controller = new ReportController(null, statementQueryService, null, null, null);
            when(statementQueryService.getStatements(eq(11L), eq(7L)))
                    .thenReturn(Mono.just(StatementsResponse.empty()));

            var result = controller.getStatements(11L, 7L).block();

            assertEquals(HttpStatus.OK, result.getStatusCode());
            StatementsResponse body = result.getBody();
            assertNotNull(body);
            assertEquals(0, body.balanceSheet().size());
            assertEquals(0, body.incomeStatement().size());
            assertEquals(0, body.cashFlow().size());
        }

        @Test
        @DisplayName("should propagate REPORT_NOT_FOUND when report missing")
        void shouldPropagateNotFoundWhenReportMissing() {
            ReportController controller = new ReportController(null, statementQueryService, null, null, null);
            when(statementQueryService.getStatements(eq(99L), eq(7L)))
                    .thenReturn(Mono.error(new BusinessException(
                            org.springframework.http.HttpStatus.NOT_FOUND,
                            "REPORT_NOT_FOUND", "报告不存在: 99")));

            StepVerifier.create(controller.getStatements(99L, 7L))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }
    }
}
