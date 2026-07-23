package com.finreport.service.artifact;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.LocalDateTime;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;
import org.springframework.http.HttpStatus;

import com.finreport.domain.entity.Report;
import com.finreport.domain.entity.ReportArtifact;
import com.finreport.exception.BusinessException;
import com.finreport.repository.ReportArtifactRepository;
import com.finreport.repository.ReportRepository;

import io.minio.GetPresignedObjectUrlArgs;
import io.minio.MinioClient;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * ArtifactQueryService 单元测试 — M3.09 + 综合审查修复。
 *
 * <p>覆盖：归属校验（report 不存在 / 不属于当前用户 → REPORT_NOT_FOUND）、
 * GENERATED 产物调 MinIO 生成预签名 URL、FAILED 产物跳过 MinIO 直接返回空 URL、
 * MinIO 抛异常时降级为空 URL、fromCallable + subscribeOn 真正把阻塞调用
 * 切到 boundedElastic（spec §12.2 性能禁忌修复回归）。</p>
 *
 * <p>MinIO {@code getPresignedObjectUrl} 声明了受检异常，测试方法统一声明
 * {@code throws Exception} 以简化 mock 设置。</p>
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
@DisplayName("ArtifactQueryService")
class ArtifactQueryServiceTest {

    @Mock
    private ReportRepository reportRepo;

    @Mock
    private ReportArtifactRepository artifactRepo;

    @Mock
    private MinioClient minioClient;

    private ArtifactQueryService service;

    private static final Long REPORT_ID = 42L;
    private static final Long USER_ID = 7L;
    private static final String PRESIGNED_URL =
            "http://minio.local/reports/42/report.pdf?X-Amz-Signature=abc";

    @BeforeEach
    void setUp() throws Exception {
        service = new ArtifactQueryService(reportRepo, artifactRepo, minioClient);
        // 默认 report 存在且属于 USER_ID
        Report report = Report.builder().id(REPORT_ID).userId(USER_ID).build();
        lenient().when(reportRepo.findById(REPORT_ID)).thenReturn(Mono.just(report));
        // 默认 MinIO 预签名成功
        lenient().when(minioClient.getPresignedObjectUrl(any(GetPresignedObjectUrlArgs.class)))
                .thenReturn(PRESIGNED_URL);
    }

    private ReportArtifact generatedArtifact(String artifactType, String objectKey) {
        return ReportArtifact.builder()
                .id(1L)
                .reportId(REPORT_ID)
                .artifactType(artifactType)
                .objectKey(objectKey)
                .status(ReportArtifact.STATUS_GENERATED)
                .createdAt(LocalDateTime.of(2026, 7, 23, 8, 0, 0))
                .build();
    }

    private ReportArtifact failedArtifact(String artifactType, String objectKey) {
        return ReportArtifact.builder()
                .id(2L)
                .reportId(REPORT_ID)
                .artifactType(artifactType)
                .objectKey(objectKey)
                .status(ReportArtifact.STATUS_FAILED)
                .createdAt(LocalDateTime.of(2026, 7, 23, 8, 0, 0))
                .build();
    }

    // ========================================================================
    // Ownership checks
    // ========================================================================

    @Nested
    @DisplayName("ownership")
    class Ownership {

        @Test
        @DisplayName("should return REPORT_NOT_FOUND when report does not exist")
        void shouldReturnNotFoundWhenReportMissing() {
            when(reportRepo.findById(99L)).thenReturn(Mono.empty());
            // listArtifacts 在组装时就会调用 findByReportIdOrderByArtifactTypeAsc，
            // Mockito 默认返回 null 会导致 NPE；stub 成空 Flux 让组装通过，
            // 真正的短路由 assertReportOwnership 的 Mono.error 实现。
            lenient().when(artifactRepo.findByReportIdOrderByArtifactTypeAsc(99L))
                    .thenReturn(Flux.empty());

            StepVerifier.create(service.listArtifacts(99L, USER_ID))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && b.getStatus() == HttpStatus.NOT_FOUND
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }

        @Test
        @DisplayName("should return REPORT_NOT_FOUND when report belongs to other user")
        void shouldReturnNotFoundWhenReportOwnedByOtherUser() {
            Report othersReport = Report.builder().id(REPORT_ID).userId(999L).build();
            when(reportRepo.findById(REPORT_ID)).thenReturn(Mono.just(othersReport));
            lenient().when(artifactRepo.findByReportIdOrderByArtifactTypeAsc(REPORT_ID))
                    .thenReturn(Flux.empty());

            StepVerifier.create(service.listArtifacts(REPORT_ID, USER_ID))
                    .expectErrorMatches(err -> err instanceof BusinessException b
                            && "REPORT_NOT_FOUND".equals(b.getErrorCode()))
                    .verify();
        }
    }

    // ========================================================================
    // URL enrichment
    // ========================================================================

    @Nested
    @DisplayName("enrichWithUrl")
    class EnrichWithUrl {

        @Test
        @DisplayName("should call MinIO and return presigned URL for GENERATED artifact")
        void shouldReturnUrlForGeneratedArtifact() throws Exception {
            ReportArtifact pdf = generatedArtifact("PDF", "reports/42/report.pdf");
            when(artifactRepo.findByReportIdOrderByArtifactTypeAsc(REPORT_ID))
                    .thenReturn(Flux.just(pdf));

            StepVerifier.create(service.listArtifacts(REPORT_ID, USER_ID))
                    .assertNext(r -> {
                        assertEquals("PDF", r.artifactType());
                        assertEquals("GENERATED", r.status());
                        assertEquals(PRESIGNED_URL, r.downloadUrl());
                    })
                    .verifyComplete();

            verify(minioClient).getPresignedObjectUrl(any(GetPresignedObjectUrlArgs.class));
        }

        @Test
        @DisplayName("should skip MinIO and return empty URL for FAILED artifact")
        void shouldReturnEmptyUrlForFailedArtifact() throws Exception {
            ReportArtifact failed = failedArtifact("CHART_BAR", "reports/42/charts/chart_bar.png");
            when(artifactRepo.findByReportIdOrderByArtifactTypeAsc(REPORT_ID))
                    .thenReturn(Flux.just(failed));

            StepVerifier.create(service.listArtifacts(REPORT_ID, USER_ID))
                    .assertNext(r -> {
                        assertEquals("FAILED", r.status());
                        assertEquals("", r.downloadUrl());
                    })
                    .verifyComplete();

            verify(minioClient, never()).getPresignedObjectUrl(any(GetPresignedObjectUrlArgs.class));
        }

        @Test
        @DisplayName("should fallback to empty URL when MinIO throws")
        void shouldFallbackWhenMinioThrows() throws Exception {
            when(minioClient.getPresignedObjectUrl(any(GetPresignedObjectUrlArgs.class)))
                    .thenThrow(new RuntimeException("MinIO unavailable"));
            ReportArtifact pdf = generatedArtifact("PDF", "reports/42/report.pdf");
            when(artifactRepo.findByReportIdOrderByArtifactTypeAsc(REPORT_ID))
                    .thenReturn(Flux.just(pdf));

            StepVerifier.create(service.listArtifacts(REPORT_ID, USER_ID))
                    .assertNext(r -> {
                        assertEquals("GENERATED", r.status());
                        assertEquals("", r.downloadUrl());
                    })
                    .verifyComplete();
        }

        @Test
        @DisplayName("should enrich each artifact independently for mixed status list")
        void shouldEnrichEachArtifactIndependently() throws Exception {
            ReportArtifact pdf = generatedArtifact("PDF", "reports/42/report.pdf");
            ReportArtifact failedMd = failedArtifact("MARKDOWN", "reports/42/report.md");
            ReportArtifact chart = generatedArtifact("CHART_PIE", "reports/42/charts/chart_pie.png");
            when(artifactRepo.findByReportIdOrderByArtifactTypeAsc(REPORT_ID))
                    .thenReturn(Flux.just(pdf, failedMd, chart));

            StepVerifier.create(service.listArtifacts(REPORT_ID, USER_ID))
                    .assertNext(r -> assertEquals(PRESIGNED_URL, r.downloadUrl()))
                    .assertNext(r -> assertEquals("", r.downloadUrl()))
                    .assertNext(r -> assertEquals(PRESIGNED_URL, r.downloadUrl()))
                    .verifyComplete();

            // 只对 2 个 GENERATED 产物调用 MinIO，FAILED 跳过
            verify(minioClient, org.mockito.Mockito.times(2))
                    .getPresignedObjectUrl(any(GetPresignedObjectUrlArgs.class));
        }

        @Test
        @DisplayName("should return empty flux when no artifacts exist")
        void shouldReturnEmptyFluxWhenNoArtifacts() throws Exception {
            when(artifactRepo.findByReportIdOrderByArtifactTypeAsc(REPORT_ID))
                    .thenReturn(Flux.empty());

            StepVerifier.create(service.listArtifacts(REPORT_ID, USER_ID))
                    .verifyComplete();

            verify(minioClient, never()).getPresignedObjectUrl(any(GetPresignedObjectUrlArgs.class));
        }

        @Test
        @DisplayName("should execute blocking MinIO call on boundedElastic scheduler")
        void shouldExecuteMinioCallOnBoundedElastic() throws Exception {
            // 验证 fromCallable 修复：阻塞调用应被 subscribeOn(boundedElastic) 切到
            // worker 线程，不在 Reactor 事件循环上执行。捕获当前线程名断言。
            final String[] threadName = {null};
            when(minioClient.getPresignedObjectUrl(any(GetPresignedObjectUrlArgs.class)))
                    .thenAnswer(inv -> {
                        threadName[0] = Thread.currentThread().getName();
                        return PRESIGNED_URL;
                    });
            ReportArtifact pdf = generatedArtifact("PDF", "reports/42/report.pdf");
            when(artifactRepo.findByReportIdOrderByArtifactTypeAsc(REPORT_ID))
                    .thenReturn(Flux.just(pdf));

            StepVerifier.create(service.listArtifacts(REPORT_ID, USER_ID))
                    .assertNext(r -> assertEquals(PRESIGNED_URL, r.downloadUrl()))
                    .verifyComplete();

            assertTrue(threadName[0] != null, "MinIO 调用应被执行");
            assertTrue(threadName[0].contains("boundedElastic"),
                    "MinIO 阻塞调用应在 boundedElastic 线程执行，实际：" + threadName[0]);
        }
    }
}
