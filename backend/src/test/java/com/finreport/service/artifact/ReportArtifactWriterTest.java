package com.finreport.service.artifact;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
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
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;
import org.springframework.transaction.reactive.TransactionalOperator;

import com.finreport.domain.entity.Report;
import com.finreport.domain.entity.ReportArtifact;
import com.finreport.domain.entity.Task;
import com.finreport.repository.ReportArtifactRepository;
import com.finreport.repository.ReportRepository;
import com.finreport.repository.TaskRepository;

import io.minio.BucketExistsArgs;
import io.minio.MakeBucketArgs;
import io.minio.MinioClient;
import io.minio.PutObjectArgs;
import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * ReportArtifactWriter 单元测试 — M3.08。
 *
 * <p>覆盖：M3.08 契约 payload 解析 / reportId 解析 / 5 个产物顺序上传 + 事务写入 /
 * 各种容错路径（空 result / pdf 缺失 / md 缺失 / charts 缺失 / chart_type 非白名单 /
 * Base64 非法 / 重放幂等 / 单文件上传失败 status=FAILED / bucket 自动创建）。</p>
 *
 * <p>MinIO 客户端通过 Mockito mock，避免本地启动 MinIO 服务；assertions 通过
 * {@link ArgumentCaptor} 捕获 {@link PutObjectArgs} 验证 object key 和 contentType。</p>
 *
 * <p>MinIO 8.5.10 的 {@code bucketExists} / {@code putObject} / {@code makeBucket}
 * 声明了 {@code throws ErrorResponseException, IOException} 等受检异常，因此凡
 * mock 这些方法的测试方法统一声明 {@code throws Exception}。</p>
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
@DisplayName("ReportArtifactWriter")
class ReportArtifactWriterTest {

    @Mock
    private TaskRepository taskRepo;

    @Mock
    private ReportRepository reportRepo;

    @Mock
    private ReportArtifactRepository artifactRepo;

    @Mock
    private MinioClient minioClient;

    @Mock
    private TransactionalOperator transactionalOperator;

    private ReportArtifactWriter writer;

    private static final byte[] SAMPLE_PDF = "%PDF-1.4\nmock pdf\n%%EOF".getBytes(StandardCharsets.UTF_8);
    private static final String SAMPLE_PDF_B64 =
            Base64.getEncoder().encodeToString(SAMPLE_PDF);
    private static final byte[] SAMPLE_PNG = {(byte) 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A};
    private static final String SAMPLE_PNG_B64 =
            Base64.getEncoder().encodeToString(SAMPLE_PNG);
    private static final String SAMPLE_MARKDOWN = "# 公司概况\n2024 年报分析。";

    @BeforeEach
    void setUp() throws Exception {
        writer = new ReportArtifactWriter(
                taskRepo, reportRepo, artifactRepo, minioClient, transactionalOperator);
        // 事务包裹默认透传：直接执行传入的 Mono，不做真实事务边界。
        lenient().doAnswer(invocation -> invocation.getArgument(0))
                .when(transactionalOperator).transactional(any(Mono.class));
        // bucket 默认存在
        lenient().when(minioClient.bucketExists(any(BucketExistsArgs.class))).thenReturn(true);
        // MinIO putObject 默认成功（不抛异常即成功）
        lenient().when(minioClient.putObject(any(PutObjectArgs.class)))
                .thenReturn(null);
        // 默认 task 不存在（用例可重写）
        lenient().when(taskRepo.findById(anyString())).thenReturn(Mono.empty());
        // 默认 artifact 不存在（非幂等命中）
        lenient().when(artifactRepo.findByReportIdAndArtifactType(anyLong(), anyString()))
                .thenReturn(Mono.empty());
        // 默认 save 返回原对象
        lenient().when(artifactRepo.save(any(ReportArtifact.class)))
                .thenAnswer(inv -> {
                    ReportArtifact a = inv.getArgument(0);
                    if (a.getId() == null) {
                        a.setId(System.nanoTime() % 1000);
                    }
                    return Mono.just(a);
                });
    }

    // ========================================================================
    // Helpers
    // ========================================================================

    private Task stubTaskWithReportId(String taskId, Long reportId) {
        Task task = Task.builder().id(taskId).refReportId(reportId).build();
        when(taskRepo.findById(taskId)).thenReturn(Mono.just(task));
        return task;
    }

    private Map<String, Object> fullPayload() {
        Map<String, Object> chart1 = new LinkedHashMap<>();
        chart1.put("chart_type", "ASSET_STRUCTURE_PIE");
        chart1.put("title", "资产结构");
        chart1.put("png_b64", SAMPLE_PNG_B64);
        Map<String, Object> chart2 = new LinkedHashMap<>();
        chart2.put("chart_type", "REVENUE_TREND_LINE");
        chart2.put("title", "营收趋势");
        chart2.put("png_b64", SAMPLE_PNG_B64);
        Map<String, Object> chart3 = new LinkedHashMap<>();
        chart3.put("chart_type", "CASH_FLOW_BAR");
        chart3.put("title", "现金流");
        chart3.put("png_b64", SAMPLE_PNG_B64);

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("operation", "REPORT");
        payload.put("status", "SUCCESS");
        payload.put("report_period", "2024-12-31");
        payload.put("markdown", SAMPLE_MARKDOWN);
        payload.put("pdf_b64", SAMPLE_PDF_B64);
        payload.put("charts", List.of(chart1, chart2, chart3));
        payload.put("fallback", false);
        return payload;
    }

    // ========================================================================
    // Payload parsing
    // ========================================================================

    @Nested
    @DisplayName("parseArtifacts")
    class ParseArtifacts {

        @Test
        @DisplayName("should parse full payload: pdf + md + 3 charts")
        void shouldParseFullPayload() {
            ReportArtifactWriter.ParsedArtifacts parsed =
                    ReportArtifactWriter.parseArtifacts(fullPayload());

            assertNotNull(parsed);
            assertNotNull(parsed.pdf());
            assertEquals(SAMPLE_PDF.length, parsed.pdf().length);
            assertEquals(SAMPLE_MARKDOWN, parsed.markdown());
            assertEquals(3, parsed.charts().size());
            assertEquals("ASSET_STRUCTURE_PIE", parsed.charts().get(0).chartType());
            assertEquals("资产结构", parsed.charts().get(0).title());
        }

        @Test
        @DisplayName("should return null when neither pdf nor markdown present")
        void shouldReturnNullWhenNoPdfNoMarkdown() {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("operation", "REPORT");
            payload.put("charts", List.of());

            assertNull(ReportArtifactWriter.parseArtifacts(payload));
        }

        @Test
        @DisplayName("should parse with only pdf (no markdown)")
        void shouldParseWithOnlyPdf() {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("pdf_b64", SAMPLE_PDF_B64);

            ReportArtifactWriter.ParsedArtifacts parsed =
                    ReportArtifactWriter.parseArtifacts(payload);

            assertNotNull(parsed);
            assertNotNull(parsed.pdf());
            assertNull(parsed.markdown());
            assertTrue(parsed.charts().isEmpty());
        }

        @Test
        @DisplayName("should parse with only markdown (no pdf)")
        void shouldParseWithOnlyMarkdown() {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("markdown", SAMPLE_MARKDOWN);

            ReportArtifactWriter.ParsedArtifacts parsed =
                    ReportArtifactWriter.parseArtifacts(payload);

            assertNotNull(parsed);
            assertNull(parsed.pdf());
            assertEquals(SAMPLE_MARKDOWN, parsed.markdown());
        }

        @Test
        @DisplayName("should skip unknown chart_type")
        void shouldSkipUnknownChartType() {
            Map<String, Object> chart = new LinkedHashMap<>();
            chart.put("chart_type", "UNKNOWN_TYPE");
            chart.put("png_b64", SAMPLE_PNG_B64);
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("pdf_b64", SAMPLE_PDF_B64);
            payload.put("charts", List.of(chart));

            ReportArtifactWriter.ParsedArtifacts parsed =
                    ReportArtifactWriter.parseArtifacts(payload);

            assertNotNull(parsed);
            assertTrue(parsed.charts().isEmpty());
        }

        @Test
        @DisplayName("should accept lowercase chart_type (L3 Pydantic value)")
        void shouldAcceptLowercaseChartType() {
            // L3 ChartType 是 str Enum，ASSET_STRUCTURE_PIE = "asset_structure_pie"
            // Pydantic model_dump() 默认序列化为 value（小写），L2 必须容错接受
            Map<String, Object> chart = new LinkedHashMap<>();
            chart.put("chart_type", "asset_structure_pie");
            chart.put("title", "资产结构");
            chart.put("png_b64", SAMPLE_PNG_B64);
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("pdf_b64", SAMPLE_PDF_B64);
            payload.put("charts", List.of(chart));

            ReportArtifactWriter.ParsedArtifacts parsed =
                    ReportArtifactWriter.parseArtifacts(payload);

            assertNotNull(parsed);
            assertEquals(1, parsed.charts().size());
            assertEquals("ASSET_STRUCTURE_PIE", parsed.charts().get(0).chartType());
        }

        @Test
        @DisplayName("should accept mixed-case chart_type (defensive)")
        void shouldAcceptMixedCaseChartType() {
            Map<String, Object> chart = new LinkedHashMap<>();
            chart.put("chart_type", "Revenue_Trend_Line");
            chart.put("png_b64", SAMPLE_PNG_B64);
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("pdf_b64", SAMPLE_PDF_B64);
            payload.put("charts", List.of(chart));

            ReportArtifactWriter.ParsedArtifacts parsed =
                    ReportArtifactWriter.parseArtifacts(payload);

            assertNotNull(parsed);
            assertEquals(1, parsed.charts().size());
            assertEquals("REVENUE_TREND_LINE", parsed.charts().get(0).chartType());
        }

        @Test
        @DisplayName("should skip chart with missing png_b64")
        void shouldSkipChartWithMissingPngB64() {
            Map<String, Object> chart = new LinkedHashMap<>();
            chart.put("chart_type", "ASSET_STRUCTURE_PIE");
            chart.put("title", "资产结构");
            // png_b64 缺失
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("pdf_b64", SAMPLE_PDF_B64);
            payload.put("charts", List.of(chart));

            ReportArtifactWriter.ParsedArtifacts parsed =
                    ReportArtifactWriter.parseArtifacts(payload);

            assertNotNull(parsed);
            assertTrue(parsed.charts().isEmpty());
        }

        @Test
        @DisplayName("should truncate charts over MAX_CHARTS (3)")
        void shouldTruncateChartsOverMax() {
            Map<String, Object> chart = new LinkedHashMap<>();
            chart.put("chart_type", "ASSET_STRUCTURE_PIE");
            chart.put("png_b64", SAMPLE_PNG_B64);
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("pdf_b64", SAMPLE_PDF_B64);
            // 5 张图表，应截断到 3
            payload.put("charts", List.of(chart, chart, chart, chart, chart));

            ReportArtifactWriter.ParsedArtifacts parsed =
                    ReportArtifactWriter.parseArtifacts(payload);

            assertNotNull(parsed);
            assertEquals(3, parsed.charts().size());
        }

        @Test
        @DisplayName("should skip invalid base64 pdf")
        void shouldSkipInvalidBase64Pdf() {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("pdf_b64", "!!!invalid base64!!!");
            payload.put("markdown", SAMPLE_MARKDOWN);

            ReportArtifactWriter.ParsedArtifacts parsed =
                    ReportArtifactWriter.parseArtifacts(payload);

            // pdf 解析失败但 markdown 仍可用
            assertNotNull(parsed);
            assertNull(parsed.pdf());
            assertEquals(SAMPLE_MARKDOWN, parsed.markdown());
        }

        @Test
        @DisplayName("should skip invalid base64 chart png")
        void shouldSkipInvalidBase64Chart() {
            Map<String, Object> chart = new LinkedHashMap<>();
            chart.put("chart_type", "ASSET_STRUCTURE_PIE");
            chart.put("png_b64", "@@invalid@@");
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("pdf_b64", SAMPLE_PDF_B64);
            payload.put("charts", List.of(chart));

            ReportArtifactWriter.ParsedArtifacts parsed =
                    ReportArtifactWriter.parseArtifacts(payload);

            assertNotNull(parsed);
            assertTrue(parsed.charts().isEmpty());
        }
    }

    // ========================================================================
    // Object key builders
    // ========================================================================

    @Nested
    @DisplayName("object keys (spec §5.3)")
    class ObjectKeys {

        @Test
        @DisplayName("pdf object key should follow reports/{reportId}/report.pdf")
        void pdfKeyShouldFollowSpec() {
            assertEquals("reports/42/report.pdf",
                    ReportArtifactWriter.buildPdfObjectKey(42L));
        }

        @Test
        @DisplayName("markdown object key should follow reports/{reportId}/report.md")
        void markdownKeyShouldFollowSpec() {
            assertEquals("reports/42/report.md",
                    ReportArtifactWriter.buildMarkdownObjectKey(42L));
        }

        @Test
        @DisplayName("chart object key should follow reports/{reportId}/charts/{filename}")
        void chartKeyShouldFollowSpec() {
            assertEquals("reports/42/charts/chart_pie.png",
                    ReportArtifactWriter.buildChartObjectKey(42L, "chart_pie.png"));
        }
    }

    // ========================================================================
    // Successful upload flows
    // ========================================================================

    @Nested
    @DisplayName("writeArtifacts success")
    class WriteArtifactsSuccess {

        @Test
        @DisplayName("should upload 5 artifacts when full payload")
        void shouldUpload5ArtifactsWhenFullPayload() throws Exception {
            stubTaskWithReportId("task-1", 42L);

            StepVerifier.create(writer.writeArtifacts("task-1", fullPayload()))
                    .expectNext(5)
                    .verifyComplete();

            // MinIO 应被调用 5 次（1 PDF + 1 MD + 3 PNG）
            verify(minioClient, org.mockito.Mockito.times(5)).putObject(any(PutObjectArgs.class));
            // artifactRepo 应被 save 5 次
            verify(artifactRepo, org.mockito.Mockito.times(5)).save(any(ReportArtifact.class));
        }

        @Test
        @DisplayName("should upload only pdf when no markdown and no charts")
        void shouldUploadOnlyPdf() throws Exception {
            stubTaskWithReportId("task-1", 42L);
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("pdf_b64", SAMPLE_PDF_B64);

            StepVerifier.create(writer.writeArtifacts("task-1", payload))
                    .expectNext(1)
                    .verifyComplete();

            verify(minioClient, org.mockito.Mockito.times(1)).putObject(any(PutObjectArgs.class));
        }

        @Test
        @DisplayName("should upload only markdown when no pdf and no charts")
        void shouldUploadOnlyMarkdown() throws Exception {
            stubTaskWithReportId("task-1", 42L);
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("markdown", SAMPLE_MARKDOWN);

            StepVerifier.create(writer.writeArtifacts("task-1", payload))
                    .expectNext(1)
                    .verifyComplete();

            verify(minioClient, org.mockito.Mockito.times(1)).putObject(any(PutObjectArgs.class));
        }

        @Test
        @DisplayName("should upload pdf + md when charts field missing")
        void shouldUploadPdfAndMdWhenChartsMissing() throws Exception {
            stubTaskWithReportId("task-1", 42L);
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("pdf_b64", SAMPLE_PDF_B64);
            payload.put("markdown", SAMPLE_MARKDOWN);

            StepVerifier.create(writer.writeArtifacts("task-1", payload))
                    .expectNext(2)
                    .verifyComplete();

            verify(minioClient, org.mockito.Mockito.times(2)).putObject(any(PutObjectArgs.class));
        }

        @Test
        @DisplayName("should use correct content types for each artifact type")
        void shouldUseCorrectContentTypes() throws Exception {
            stubTaskWithReportId("task-1", 42L);

            writer.writeArtifacts("task-1", fullPayload()).block();

            ArgumentCaptor<PutObjectArgs> captor = ArgumentCaptor.forClass(PutObjectArgs.class);
            verify(minioClient, org.mockito.Mockito.times(5)).putObject(captor.capture());

            List<PutObjectArgs> allCalls = captor.getAllValues();
            // 验证 content types（顺序：PDF, MD, Chart1, Chart2, Chart3）
            assertEquals("application/pdf", allCalls.get(0).contentType());
            assertEquals("text/markdown; charset=utf-8", allCalls.get(1).contentType());
            assertEquals("image/png", allCalls.get(2).contentType());
            assertEquals("image/png", allCalls.get(3).contentType());
            assertEquals("image/png", allCalls.get(4).contentType());
        }

        @Test
        @DisplayName("should resolve reportId via report.task_id when task.ref_report_id is null")
        void shouldResolveReportIdViaReportTaskId() throws Exception {
            // task 没有 refReportId，但 report.task_id 关联到 task
            Task task = Task.builder().id("task-1").refReportId(null).build();
            when(taskRepo.findById("task-1")).thenReturn(Mono.just(task));
            Report report = Report.builder().id(99L).taskId("task-1").build();
            when(reportRepo.findByTaskId("task-1")).thenReturn(Mono.just(report));

            StepVerifier.create(writer.writeArtifacts("task-1", fullPayload()))
                    .expectNext(5)
                    .verifyComplete();

            verify(reportRepo).findByTaskId("task-1");
        }
    }

    // ========================================================================
    // Failure / edge cases
    // ========================================================================

    @Nested
    @DisplayName("writeArtifacts failure / edge cases")
    class WriteArtifactsFailure {

        @Test
        @DisplayName("should return 0 when result is null")
        void shouldReturnZeroWhenResultNull() throws Exception {
            StepVerifier.create(writer.writeArtifacts("task-1", null))
                    .expectNext(0)
                    .verifyComplete();

            verify(minioClient, never()).putObject(any(PutObjectArgs.class));
            verify(artifactRepo, never()).save(any(ReportArtifact.class));
        }

        @Test
        @DisplayName("should return 0 when result is empty")
        void shouldReturnZeroWhenResultEmpty() {
            StepVerifier.create(writer.writeArtifacts("task-1", Map.of()))
                    .expectNext(0)
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return 0 when parseArtifacts fails")
        void shouldReturnZeroWhenParseFails() {
            // 既无 pdf 也无 markdown → parse 返回 null
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("operation", "REPORT");
            payload.put("charts", List.of());

            StepVerifier.create(writer.writeArtifacts("task-1", payload))
                    .expectNext(0)
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return 0 when task not found")
        void shouldReturnZeroWhenTaskNotFound() throws Exception {
            // 默认 taskRepo.findById 返回 empty

            StepVerifier.create(writer.writeArtifacts("nonexistent-task", fullPayload()))
                    .expectNext(0)
                    .verifyComplete();

            verify(minioClient, never()).putObject(any(PutObjectArgs.class));
        }

        @Test
        @DisplayName("should mark PDF artifact FAILED when MinIO upload throws")
        void shouldMarkPdfFailedWhenMinioThrows() throws Exception {
            stubTaskWithReportId("task-1", 42L);
            // PDF 上传抛异常，其他仍应成功。
            // 注意：PutObjectArgs.contentType() 在 minio 8.5.10 声明了
            // throws NoSuchAlgorithmException, IOException, KeyManagementException,
            // 不能在 lambda 内部调用；改用 args.object() 匹配 PDF 路径。
            org.mockito.Mockito.doThrow(new RuntimeException("MinIO down"))
                    .when(minioClient).putObject(org.mockito.ArgumentMatchers.argThat(
                            (PutObjectArgs args) -> args.object().endsWith("report.pdf")));

            StepVerifier.create(writer.writeArtifacts("task-1", fullPayload()))
                    .expectNext(5)
                    .verifyComplete();

            // 验证 PDF artifact 的 status=FAILED
            ArgumentCaptor<ReportArtifact> captor = ArgumentCaptor.forClass(ReportArtifact.class);
            verify(artifactRepo, org.mockito.Mockito.times(5)).save(captor.capture());
            List<ReportArtifact> saved = captor.getAllValues();
            ReportArtifact pdfArtifact = saved.stream()
                    .filter(a -> "PDF".equals(a.getArtifactType()))
                    .findFirst()
                    .orElseThrow();
            assertEquals(ReportArtifact.STATUS_FAILED, pdfArtifact.getStatus());
        }

        @Test
        @DisplayName("should mark chart artifact FAILED when MinIO upload throws")
        void shouldMarkChartFailedWhenMinioThrows() throws Exception {
            stubTaskWithReportId("task-1", 42L);
            // 只让 chart_pie 上传失败。
            // 用 args.object() 匹配（contentType() 在 lambda 内不可用，详见 shouldMarkPdfFailed）。
            org.mockito.Mockito.doThrow(new RuntimeException("chart upload failed"))
                    .when(minioClient).putObject(org.mockito.ArgumentMatchers.argThat(
                            (PutObjectArgs args) -> args.object().endsWith("chart_pie.png")));

            StepVerifier.create(writer.writeArtifacts("task-1", fullPayload()))
                    .expectNext(5)
                    .verifyComplete();

            ArgumentCaptor<ReportArtifact> captor = ArgumentCaptor.forClass(ReportArtifact.class);
            verify(artifactRepo, org.mockito.Mockito.times(5)).save(captor.capture());
            List<ReportArtifact> saved = captor.getAllValues();
            ReportArtifact pieArtifact = saved.stream()
                    .filter(a -> "CHART_PIE".equals(a.getArtifactType()))
                    .findFirst()
                    .orElseThrow();
            assertEquals(ReportArtifact.STATUS_FAILED, pieArtifact.getStatus());

            // 其他图表应仍为 GENERATED
            ReportArtifact lineArtifact = saved.stream()
                    .filter(a -> "CHART_LINE".equals(a.getArtifactType()))
                    .findFirst()
                    .orElseThrow();
            assertEquals(ReportArtifact.STATUS_GENERATED, lineArtifact.getStatus());
        }

        @Test
        @DisplayName("should not propagate error when MinIO upload fails entirely")
        void shouldNotPropagateErrorWhenAllUploadsFail() throws Exception {
            stubTaskWithReportId("task-1", 42L);
            org.mockito.Mockito.doThrow(new RuntimeException("MinIO down"))
                    .when(minioClient).putObject(any(PutObjectArgs.class));

            StepVerifier.create(writer.writeArtifacts("task-1", fullPayload()))
                    .expectNext(5)
                    .verifyComplete();

            // 所有 5 个 artifact 仍写入表，status 全为 FAILED
            ArgumentCaptor<ReportArtifact> captor = ArgumentCaptor.forClass(ReportArtifact.class);
            verify(artifactRepo, org.mockito.Mockito.times(5)).save(captor.capture());
            for (ReportArtifact a : captor.getAllValues()) {
                assertEquals(ReportArtifact.STATUS_FAILED, a.getStatus());
            }
        }
    }

    // ========================================================================
    // Idempotency
    // ========================================================================

    @Nested
    @DisplayName("idempotency")
    class Idempotency {

        @Test
        @DisplayName("should skip when artifact already GENERATED (replay)")
        void shouldSkipWhenArtifactAlreadyGenerated() throws Exception {
            stubTaskWithReportId("task-1", 42L);
            // 模拟已存在 GENERATED 行
            ReportArtifact existing = ReportArtifact.builder()
                    .id(1L)
                    .reportId(42L)
                    .artifactType("PDF")
                    .objectKey("reports/42/report.pdf")
                    .status(ReportArtifact.STATUS_GENERATED)
                    .build();
            when(artifactRepo.findByReportIdAndArtifactType(42L, "PDF"))
                    .thenReturn(Mono.just(existing));
            // 其他类型仍不存在
            // setUp 默认 lenient stub 已覆盖

            StepVerifier.create(writer.writeArtifacts("task-1", fullPayload()))
                    // PDF 幂等命中跳过，剩 4 个写入
                    .expectNext(4)
                    .verifyComplete();

            // MinIO 上传仍执行 5 次（幂等检查发生在上传之后，因为上传是 best-effort）
            verify(minioClient, org.mockito.Mockito.times(5)).putObject(any(PutObjectArgs.class));
        }

        @Test
        @DisplayName("should overwrite when artifact previously FAILED (retry)")
        void shouldOverwriteWhenArtifactPreviouslyFailed() {
            stubTaskWithReportId("task-1", 42L);
            ReportArtifact existing = ReportArtifact.builder()
                    .id(1L)
                    .reportId(42L)
                    .artifactType("PDF")
                    .objectKey("reports/42/report.pdf")
                    .status(ReportArtifact.STATUS_FAILED)
                    .build();
            when(artifactRepo.findByReportIdAndArtifactType(42L, "PDF"))
                    .thenReturn(Mono.just(existing));

            StepVerifier.create(writer.writeArtifacts("task-1", fullPayload()))
                    .expectNext(5)
                    .verifyComplete();

            // 验证 existing 被 save（覆盖 FAILED 行 → GENERATED）
            verify(artifactRepo).save(org.mockito.ArgumentMatchers.argThat(
                    (ReportArtifact a) -> a.getId() != null && a.getId().equals(1L)
                            && "GENERATED".equals(a.getStatus())));
        }
    }

    // ========================================================================
    // Bucket creation
    // ========================================================================

    @Nested
    @DisplayName("bucket creation")
    class BucketCreation {

        @Test
        @DisplayName("should create bucket when not exists")
        void shouldCreateBucketWhenNotExists() throws Exception {
            stubTaskWithReportId("task-1", 42L);
            when(minioClient.bucketExists(any(BucketExistsArgs.class))).thenReturn(false);

            writer.writeArtifacts("task-1", fullPayload()).block();

            verify(minioClient).makeBucket(any(MakeBucketArgs.class));
        }

        @Test
        @DisplayName("should not create bucket when already exists")
        void shouldNotCreateBucketWhenAlreadyExists() throws Exception {
            stubTaskWithReportId("task-1", 42L);
            when(minioClient.bucketExists(any(BucketExistsArgs.class))).thenReturn(true);

            writer.writeArtifacts("task-1", fullPayload()).block();

            verify(minioClient, never()).makeBucket(any(MakeBucketArgs.class));
        }

        @Test
        @DisplayName("should swallow BucketAlreadyOwnedByYou error")
        void shouldSwallowBucketAlreadyOwnedByYouError() throws Exception {
            stubTaskWithReportId("task-1", 42L);
            when(minioClient.bucketExists(any(BucketExistsArgs.class))).thenReturn(false);

            // MinIO 8.5.10: ErrorResponseException 是 final class，但 Mockito 5 inline
            // mock-maker 支持最终类。mock ErrorResponseException + 内嵌 ErrorResponse，
            // 让 errorResponse().code() 返回 "BucketAlreadyOwnedByYou"，触发吞掉逻辑。
            io.minio.messages.ErrorResponse errorResponse =
                    org.mockito.Mockito.mock(io.minio.messages.ErrorResponse.class);
            when(errorResponse.code()).thenReturn("BucketAlreadyOwnedByYou");
            io.minio.errors.ErrorResponseException ex =
                    org.mockito.Mockito.mock(io.minio.errors.ErrorResponseException.class);
            when(ex.errorResponse()).thenReturn(errorResponse);
            org.mockito.Mockito.doThrow(ex)
                    .when(minioClient).makeBucket(any(MakeBucketArgs.class));

            // 不抛异常即成功（bucket 已存在分支被吞掉）
            writer.writeArtifacts("task-1", fullPayload()).block();

            verify(minioClient).makeBucket(any(MakeBucketArgs.class));
            // 5 个 artifact 仍正常上传
            verify(minioClient, org.mockito.Mockito.times(5)).putObject(any(PutObjectArgs.class));
        }
    }

    // ========================================================================
    // Object key in artifacts
    // ========================================================================

    @Nested
    @DisplayName("artifact object keys (spec §5.3)")
    class ArtifactObjectKeys {

        @Test
        @DisplayName("should write report_artifact rows with spec-compliant object keys")
        void shouldWriteArtifactsWithSpecKeys() {
            stubTaskWithReportId("task-1", 42L);

            writer.writeArtifacts("task-1", fullPayload()).block();

            ArgumentCaptor<ReportArtifact> captor = ArgumentCaptor.forClass(ReportArtifact.class);
            verify(artifactRepo, org.mockito.Mockito.times(5)).save(captor.capture());

            List<ReportArtifact> saved = captor.getAllValues();
            // 验证每行 object key 符合 spec §5.3
            for (ReportArtifact a : saved) {
                assertTrue(a.getObjectKey().startsWith("reports/42/"),
                        "object key should start with reports/42/: " + a.getObjectKey());
            }

            // PDF / MD / charts 各自的 key 格式
            ReportArtifact pdf = saved.stream().filter(a -> "PDF".equals(a.getArtifactType())).findFirst().orElseThrow();
            assertEquals("reports/42/report.pdf", pdf.getObjectKey());

            ReportArtifact md = saved.stream().filter(a -> "MARKDOWN".equals(a.getArtifactType())).findFirst().orElseThrow();
            assertEquals("reports/42/report.md", md.getObjectKey());

            ReportArtifact pie = saved.stream().filter(a -> "CHART_PIE".equals(a.getArtifactType())).findFirst().orElseThrow();
            assertEquals("reports/42/charts/chart_pie.png", pie.getObjectKey());

            ReportArtifact line = saved.stream().filter(a -> "CHART_LINE".equals(a.getArtifactType())).findFirst().orElseThrow();
            assertEquals("reports/42/charts/chart_line.png", line.getObjectKey());

            ReportArtifact bar = saved.stream().filter(a -> "CHART_BAR".equals(a.getArtifactType())).findFirst().orElseThrow();
            assertEquals("reports/42/charts/chart_bar.png", bar.getObjectKey());

            // 所有行的 reportId 应为 42
            for (ReportArtifact a : saved) {
                assertEquals(42L, a.getReportId());
                assertEquals(ReportArtifact.STATUS_GENERATED, a.getStatus());
            }
        }
    }
}
