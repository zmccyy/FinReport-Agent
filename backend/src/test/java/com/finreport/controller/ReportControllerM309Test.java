package com.finreport.controller;

import static org.junit.jupiter.api.Assertions.assertEquals;
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

import com.finreport.domain.dto.AccountingCheckResponse;
import com.finreport.domain.dto.AnomalyResponse;
import com.finreport.domain.dto.ReportArtifactResponse;
import com.finreport.exception.BusinessException;
import com.finreport.service.artifact.ArtifactQueryService;
import com.finreport.service.reasoner.AnomalyQueryService;
import com.finreport.service.reasoner.CheckQueryService;

import reactor.core.publisher.Flux;
import reactor.test.StepVerifier;

/**
 * ReportController M3.09 查询端点单测 — checks / anomalies / artifacts。
 *
 * <p>验证 controller 薄壳：参数透传 + Flux 输出，归属校验与业务排序由对应
 * Service 完成。</p>
 */
@ExtendWith(MockitoExtension.class)
@DisplayName("ReportController M3.09 check/anomaly/artifact endpoints")
class ReportControllerM309Test {

    @Mock
    private CheckQueryService checkQueryService;

    @Mock
    private AnomalyQueryService anomalyQueryService;

    @Mock
    private ArtifactQueryService artifactQueryService;

    private ReportController newController() {
        return new ReportController(null, null, checkQueryService, anomalyQueryService, artifactQueryService);
    }

    @Nested
    @DisplayName("GET /reports/{reportId}/checks")
    class GetChecks {

        @Test
        @DisplayName("should return flux of check responses when report owned")
        void shouldReturnChecksWhenOwned() {
            ReportController controller = newController();
            AccountingCheckResponse check = new AccountingCheckResponse(
                    1L, "资产负债表恒等式", "balance_sheet_identity",
                    new BigDecimal("2800000000.00"), new BigDecimal("2800000000.00"),
                    BigDecimal.ZERO, true, "INFO", "资产 = 负债 + 权益",
                    Instant.parse("2026-07-23T08:00:00Z"));
            when(checkQueryService.listChecks(eq(11L), eq(7L))).thenReturn(Flux.just(check));

            StepVerifier.create(controller.getChecks(11L, 7L))
                    .assertNext(r -> {
                        assertEquals("balance_sheet_identity", r.ruleType());
                        assertEquals(true, r.isPass());
                        assertEquals("INFO", r.severity());
                    })
                    .verifyComplete();
        }

        @Test
        @DisplayName("should propagate REPORT_NOT_FOUND when report does not exist")
        void shouldPropagateNotFoundWhenReportMissing() {
            ReportController controller = newController();
            when(checkQueryService.listChecks(eq(99L), eq(7L)))
                    .thenReturn(Flux.error(new BusinessException(
                            org.springframework.http.HttpStatus.NOT_FOUND,
                            "REPORT_NOT_FOUND", "报告不存在: 99")));

            StepVerifier.create(controller.getChecks(99L, 7L))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }
    }

    @Nested
    @DisplayName("GET /reports/{reportId}/anomalies")
    class GetAnomalies {

        @Test
        @DisplayName("should return flux of anomaly responses when report owned")
        void shouldReturnAnomaliesWhenOwned() {
            ReportController controller = newController();
            AnomalyResponse anomaly = new AnomalyResponse(
                    2L, "营业收入", "yoy_change",
                    new BigDecimal("0.35"), new BigDecimal("0.30"),
                    "营业收入同比增长 35%，超过阈值 30%", "WARN",
                    Instant.parse("2026-07-23T08:00:00Z"));
            when(anomalyQueryService.listAnomalies(eq(11L), eq(7L))).thenReturn(Flux.just(anomaly));

            StepVerifier.create(controller.getAnomalies(11L, 7L))
                    .assertNext(r -> {
                        assertEquals("营业收入", r.itemName());
                        assertEquals("WARN", r.severity());
                        assertEquals("yoy_change", r.anomalyType());
                    })
                    .verifyComplete();
        }

        @Test
        @DisplayName("should propagate REPORT_NOT_FOUND when report does not exist")
        void shouldPropagateNotFoundWhenReportMissing() {
            ReportController controller = newController();
            when(anomalyQueryService.listAnomalies(eq(99L), eq(7L)))
                    .thenReturn(Flux.error(new BusinessException(
                            org.springframework.http.HttpStatus.NOT_FOUND,
                            "REPORT_NOT_FOUND", "报告不存在: 99")));

            StepVerifier.create(controller.getAnomalies(99L, 7L))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }
    }

    @Nested
    @DisplayName("GET /reports/{reportId}/artifacts")
    class GetArtifacts {

        @Test
        @DisplayName("should return flux of artifact responses with download urls")
        void shouldReturnArtifactsWithUrls() {
            ReportController controller = newController();
            ReportArtifactResponse pdf = new ReportArtifactResponse(
                    3L, "PDF", "reports/11/report.pdf", "GENERATED",
                    "http://minio.local/reports/11/report.pdf?X-Amz-Signature=abc",
                    Instant.parse("2026-07-23T08:00:00Z"));
            ReportArtifactResponse md = new ReportArtifactResponse(
                    4L, "MARKDOWN", "reports/11/report.md", "GENERATED",
                    "http://minio.local/reports/11/report.md?X-Amz-Signature=def",
                    Instant.parse("2026-07-23T08:00:00Z"));
            ReportArtifactResponse chart = new ReportArtifactResponse(
                    5L, "CHART_PIE", "reports/11/charts/chart_pie.png", "GENERATED",
                    "http://minio.local/reports/11/charts/chart_pie.png?X-Amz-Signature=ghi",
                    Instant.parse("2026-07-23T08:00:00Z"));
            when(artifactQueryService.listArtifacts(eq(11L), eq(7L)))
                    .thenReturn(Flux.fromIterable(List.of(pdf, md, chart)));

            StepVerifier.create(controller.getArtifacts(11L, 7L))
                    .assertNext(r -> {
                        assertEquals("PDF", r.artifactType());
                        assertEquals("GENERATED", r.status());
                        assertEquals(pdf.downloadUrl(), r.downloadUrl());
                    })
                    .assertNext(r -> assertEquals("MARKDOWN", r.artifactType()))
                    .assertNext(r -> assertEquals("CHART_PIE", r.artifactType()))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should propagate REPORT_NOT_FOUND when report does not exist")
        void shouldPropagateNotFoundWhenReportMissing() {
            ReportController controller = newController();
            when(artifactQueryService.listArtifacts(eq(99L), eq(7L)))
                    .thenReturn(Flux.error(new BusinessException(
                            org.springframework.http.HttpStatus.NOT_FOUND,
                            "REPORT_NOT_FOUND", "报告不存在: 99")));

            StepVerifier.create(controller.getArtifacts(99L, 7L))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }

        @Test
        @DisplayName("should include empty download url for failed artifacts")
        void shouldIncludeEmptyUrlForFailedArtifacts() {
            ReportController controller = newController();
            ReportArtifactResponse failedChart = new ReportArtifactResponse(
                    6L, "CHART_BAR", "reports/11/charts/chart_bar.png", "FAILED", "",
                    Instant.parse("2026-07-23T08:00:00Z"));
            when(artifactQueryService.listArtifacts(eq(11L), eq(7L)))
                    .thenReturn(Flux.just(failedChart));

            StepVerifier.create(controller.getArtifacts(11L, 7L))
                    .assertNext(r -> {
                        assertEquals("FAILED", r.status());
                        assertEquals("", r.downloadUrl());
                    })
                    .verifyComplete();
        }
    }
}
