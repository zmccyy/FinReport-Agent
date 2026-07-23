package com.finreport.service.artifact;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.reactive.TransactionalOperator;

import com.finreport.domain.entity.Report;
import com.finreport.domain.entity.ReportArtifact;
import com.finreport.repository.ReportArtifactRepository;
import com.finreport.repository.ReportRepository;
import com.finreport.repository.TaskRepository;

import io.minio.BucketExistsArgs;
import io.minio.MakeBucketArgs;
import io.minio.MinioClient;
import io.minio.PutObjectArgs;
import io.minio.errors.ErrorResponseException;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/**
 * 报告产物写入器 — spec §2.3 M10 + plan §M3.08。
 *
 * <p>消费 L3 REPORT progress 携带的 {@code result} payload，把生成的 PDF /
 * Markdown / 3 张图表 PNG 上传到 MinIO {@code finreport-reports} bucket，
 * 并在 {@code report_artifact} 表中记录 object key。{@code TaskOrchestrator
 * .handleStepSuccess} 在 REPORT 首次 SUCCESS 时调用本类，先于推进到
 * COMPLETED 状态（spec §3.2.1：REPORT_SUCCESS → COMPLETED）。</p>
 *
 * <p><b>失败策略</b>（spec §8.4 失败不强制回滚）：单文件上传失败只 log warn
 * 并跳过该产物，但 {@code report_artifact} 表仍写一行 {@code status=FAILED}
 * 保留排查痕迹；不抛异常，避免阻断状态机推进。Report → COMPLETED 链路
 * 即使产物部分失败也能完成；前端 ReportViewer 通过 artifact_type 查询下载
 * 链接时遇到 {@code status=FAILED} 会展示「产物不可用」提示。</p>
 *
 * <p><b>幂等性</b>：L3 progress 携带 {@code idempotencyKey=taskId:step}，
 * {@code TaskOrchestrator.handleStepSuccess} 已对重放 SUCCESS 做去重
 * （reconcile 路径不调本类）。本类额外做 (reportId, artifactType) 唯一性
 * 检查：重放时已存在 GENERATED 行则跳过，避免 MinIO 重复上传 + 表数据膨胀。</p>
 *
 * <p><b>payload 形状</b>（M3.08 契约，对齐 L3 {@code ReportResult} +
 * {@code PdfResult} + {@code ChartResult} 字段，bytes 经 Base64 编码进 JSON）：
 * <pre>{@code
 * {
 *   "operation": "REPORT",
 *   "status": "SUCCESS",
 *   "report_period": "2024-12-31",
 *   "markdown": "# 公司概况\n...",
 *   "pdf_b64": "JVBERi0xLjQK...",      // PdfResult.pdf_bytes Base64
 *   "charts": [
 *     {
 *       "chart_type": "ASSET_STRUCTURE_PIE",
 *       "title": "资产结构",
 *       "png_b64": "iVBORw0KGgo..."    // ChartResult.png_bytes Base64
 *     },
 *     {"chart_type": "REVENUE_TREND_LINE", "title": "营收趋势", "png_b64": "..."},
 *     {"chart_type": "CASH_FLOW_BAR", "title": "现金流", "png_b64": "..."}
 *   ],
 *   "fallback": false                  // PdfResult.fallback（仅供参考，不影响产物写入）
 * }
 * }</pre></p>
 *
 * <p><b>事务边界</b>（spec §8.4 任务边界 = 事务边界）：所有 {@code report_artifact}
 * 行在同一事务内顺序写入，任一行写入失败整体回滚，避免半成品数据。MinIO 上传
 * 在事务外执行（spec §8.4：MinIO 失败不强制回滚，保留半成品 object 供排查）。</p>
 *
 * <p><b>object key 命名</b>（spec §5.3）：
 * <ul>
 *   <li>{@code reports/{reportId}/report.pdf}</li>
 *   <li>{@code reports/{reportId}/report.md}</li>
 *   <li>{@code reports/{reportId}/charts/chart_pie.png}</li>
 *   <li>{@code reports/{reportId}/charts/chart_line.png}</li>
 *   <li>{@code reports/{reportId}/charts/chart_bar.png}</li>
 * </ul>
 * {@code finreport-reports} bucket 是 public-read 策略，预签名 URL 可直接下载。</p>
 */
@Service
public class ReportArtifactWriter {

    private static final Logger log = LoggerFactory.getLogger(ReportArtifactWriter.class);

    /**
     * 报告产物 bucket — spec §5.5.1 {@code finreport-reports}（public-read）。
     */
    static final String BUCKET = "finreport-reports";

    /**
     * 最大图表数 — spec §2.3 M10 固定 3 张图表。
     * 超过的图表在入口截断，避免 schema 字段超长 + MinIO 重复上传。
     */
    static final int MAX_CHARTS = 3;

    /**
     * 图表 chart_type → artifact_type 映射表（spec §2.3 M10）。
     */
    private static final Map<String, String> CHART_TYPE_TO_ARTIFACT_TYPE = Map.of(
            "ASSET_STRUCTURE_PIE", ReportArtifact.TYPE_CHART_PIE,
            "REVENUE_TREND_LINE", ReportArtifact.TYPE_CHART_LINE,
            "CASH_FLOW_BAR", ReportArtifact.TYPE_CHART_BAR);

    /**
     * 图表 artifact_type → object key 文件名映射（spec §5.3）。
     */
    private static final Map<String, String> CHART_ARTIFACT_TYPE_TO_FILENAME = Map.of(
            ReportArtifact.TYPE_CHART_PIE, "chart_pie.png",
            ReportArtifact.TYPE_CHART_LINE, "chart_line.png",
            ReportArtifact.TYPE_CHART_BAR, "chart_bar.png");

    /**
     * 合法的 chart_type 白名单 — 非白名单值跳过并 log warn。
     */
    private static final Set<String> VALID_CHART_TYPES = CHART_TYPE_TO_ARTIFACT_TYPE.keySet();

    private final TaskRepository taskRepo;
    private final ReportRepository reportRepo;
    private final ReportArtifactRepository artifactRepo;
    private final MinioClient minioClient;
    private final TransactionalOperator transactionalOperator;

    /**
     * 构造报告产物写入器。
     *
     * @param taskRepo            任务仓库（解析 reportId）
     * @param reportRepo          报告仓库（task.ref_report_id 缺失时回退查询）
     * @param artifactRepo        产物仓库
     * @param minioClient         MinIO 客户端
     * @param transactionalOperator 事务操作器（包裹产物写入事务）
     */
    public ReportArtifactWriter(
            TaskRepository taskRepo,
            ReportRepository reportRepo,
            ReportArtifactRepository artifactRepo,
            MinioClient minioClient,
            TransactionalOperator transactionalOperator) {
        this.taskRepo = taskRepo;
        this.reportRepo = reportRepo;
        this.artifactRepo = artifactRepo;
        this.minioClient = minioClient;
        this.transactionalOperator = transactionalOperator;
    }

    /**
     * 把一条 REPORT progress 的 result payload 解码、上传 MinIO、写 report_artifact 表。
     *
     * @param taskId 任务 ID
     * @param result L3 progress 携带的 result payload（含 markdown / pdf_b64 / charts）
     * @return 写入的产物行数（PDF + MD + N 张图表）；解析失败或无数据返回 0
     */
    public Mono<Integer> writeArtifacts(String taskId, Map<String, Object> result) {
        log.debug("[ReportArtifactWriter] writeArtifacts taskId={} resultKeys={}",
                taskId, result == null ? "[]" : result.keySet());

        if (result == null || result.isEmpty()) {
            log.warn("[ReportArtifactWriter] 空 result，跳过写库 taskId={}", taskId);
            return Mono.just(0);
        }

        ParsedArtifacts parsed = parseArtifacts(result);
        if (parsed == null) {
            log.warn("[ReportArtifactWriter] result 解析失败，跳过写库 taskId={}", taskId);
            return Mono.just(0);
        }

        return resolveReportId(taskId)
                .map(reportId -> {
                    if (reportId == null) {
                        log.error("[ReportArtifactWriter] 无法解析 reportId，跳过写库 taskId={}", taskId);
                    }
                    return reportId;
                })
                .flatMap(reportId -> {
                    if (reportId == null) {
                        return Mono.just(0);
                    }
                    return ensureBucketExists()
                            .then(uploadAndPersistAll(reportId, parsed))
                            .doOnSuccess(count -> log.info(
                                    "[ReportArtifactWriter] 写入成功 taskId={} reportId={} pdf={} md={} "
                                            + "charts={} total={}",
                                    taskId, reportId, parsed.pdf() != null, parsed.markdown() != null,
                                    parsed.charts().size(), count))
                            .onErrorResume(error -> {
                                log.error("[ReportArtifactWriter] 写入失败 taskId={} reportId={}",
                                        taskId, reportId, error);
                                return Mono.just(0);
                            });
                })
                .switchIfEmpty(Mono.defer(() -> {
                    log.error("[ReportArtifactWriter] task 不存在或无 reportId 关联，跳过写库 taskId={}",
                            taskId);
                    return Mono.just(0);
                }));
    }

    // ========================================================================
    // Parsing
    // ========================================================================

    /**
     * 从 result payload 解析 PDF / Markdown / charts 列表。
     *
     * <p>容错策略：
     * <ul>
     *   <li>{@code markdown} 字段缺失或空 → 不上传 Markdown 产物（PDF 仍可上传）</li>
     *   <li>{@code pdf_b64} 字段缺失或空 → 不上传 PDF 产物（MD 仍可上传）</li>
     *   <li>两者都缺失 → 返回 null，整体跳过</li>
     *   <li>{@code charts} 字段缺失视为空列表；超过 {@link #MAX_CHARTS} 截断</li>
     *   <li>chart_type 非白名单或 png_b64 缺失 → 跳过该图表</li>
     * </ul>
     * </p>
     */
    static ParsedArtifacts parseArtifacts(Map<String, Object> result) {
        byte[] pdfBytes = decodeBase64(stringOrNull(result.get("pdf_b64")));
        String markdown = stringOrNull(result.get("markdown"));

        if (pdfBytes == null && markdown == null) {
            return null;
        }

        List<ParsedChart> charts = new ArrayList<>();
        Object chartsObj = result.get("charts");
        if (chartsObj instanceof List<?> chartsList) {
            for (Object chartObj : chartsList) {
                if (charts.size() >= MAX_CHARTS) {
                    log.warn("[ReportArtifactWriter] charts 超过 {} 张，截断", MAX_CHARTS);
                    break;
                }
                if (!(chartObj instanceof Map<?, ?> chartMap)) {
                    continue;
                }
                ParsedChart chart = parseChart(chartMap);
                if (chart != null) {
                    charts.add(chart);
                }
            }
        }

        return new ParsedArtifacts(pdfBytes, markdown, charts);
    }

    private static ParsedChart parseChart(Map<?, ?> chartMap) {
        String chartTypeRaw = stringOrNull(chartMap.get("chart_type"));
        if (chartTypeRaw == null) {
            log.warn("[ReportArtifactWriter] chart_type 缺失，跳过");
            return null;
        }
        // 容错：L3 ChartType.value 是小写（如 "asset_structure_pie"），
        // ChartType.name 是大写（如 "ASSET_STRUCTURE_PIE"）。Pydantic 默认
        // model_dump() 序列化为 value（小写），而 L2 端 CHART_TYPE_TO_ARTIFACT_TYPE
        // 用大写 enum name 作为 key。统一转大写匹配，兼容 L3 端两种序列化方式
        // 与未来 L9 Agent 编排可能产生的混合大小写输入。
        String chartType = chartTypeRaw.toUpperCase(Locale.ROOT);
        if (!VALID_CHART_TYPES.contains(chartType)) {
            log.warn("[ReportArtifactWriter] 未知 chart_type={}，跳过", chartTypeRaw);
            return null;
        }
        byte[] pngBytes = decodeBase64(stringOrNull(chartMap.get("png_b64")));
        if (pngBytes == null) {
            log.warn("[ReportArtifactWriter] chart png_b64 缺失或非法，跳过 chart_type={}", chartType);
            return null;
        }
        String title = stringOrDefault(chartMap.get("title"), "");
        return new ParsedChart(chartType, title, pngBytes);
    }

    private static String stringOrNull(Object value) {
        if (value instanceof String s && !s.isBlank()) {
            return s;
        }
        return null;
    }

    private static String stringOrDefault(Object value, String fallback) {
        if (value instanceof String s && !s.isBlank()) {
            return s;
        }
        return fallback;
    }

    /**
     * 解码 Base64 字符串为字节数组。
     *
     * <p>对齐 L3 {@code base64.b64encode(...).decode("ascii")} 编码方式。
     * 容错：null / 空字符串 / 非法 Base64 都返回 null。</p>
     */
    private static byte[] decodeBase64(String b64) {
        if (b64 == null || b64.isEmpty()) {
            return null;
        }
        try {
            byte[] decoded = Base64.getDecoder().decode(b64.trim());
            return decoded.length == 0 ? null : decoded;
        } catch (IllegalArgumentException e) {
            log.warn("[ReportArtifactWriter] Base64 解码失败 len={}", b64.length());
            return null;
        }
    }

    // ========================================================================
    // MinIO upload + DB persist
    // ========================================================================

    /**
     * 顺序上传 PDF / Markdown / 每张图表 PNG，并在事务内写 report_artifact 表。
     *
     * <p>顺序执行而非并行：5 个文件规模下顺序执行毫秒级完成，事务内并行 save
     * 可能产生连接池压力且无显著收益。单文件上传失败不阻断后续文件
     * （spec §8.4 失败不强制回滚）。</p>
     */
    private Mono<Integer> uploadAndPersistAll(Long reportId, ParsedArtifacts parsed) {
        List<ReportArtifact> artifacts = new ArrayList<>();

        // 1. PDF
        if (parsed.pdf() != null) {
            String objectKey = buildPdfObjectKey(reportId);
            artifacts.add(buildArtifact(reportId, ReportArtifact.TYPE_PDF, objectKey));
        }

        // 2. Markdown
        if (parsed.markdown() != null) {
            String objectKey = buildMarkdownObjectKey(reportId);
            artifacts.add(buildArtifact(reportId, ReportArtifact.TYPE_MARKDOWN, objectKey));
        }

        // 3. Charts
        for (ParsedChart chart : parsed.charts()) {
            String artifactType = CHART_TYPE_TO_ARTIFACT_TYPE.get(chart.chartType());
            String filename = CHART_ARTIFACT_TYPE_TO_FILENAME.get(artifactType);
            String objectKey = buildChartObjectKey(reportId, filename);
            artifacts.add(buildArtifact(reportId, artifactType, objectKey));
        }

        if (artifacts.isEmpty()) {
            return Mono.just(0);
        }

        // 先顺序上传 MinIO（每个文件独立失败容错），再在事务内批量写表
        return uploadToMinioWithFallback(reportId, parsed, artifacts)
                .flatMap(finalArtifacts -> persistArtifacts(finalArtifacts));
    }

    /**
     * 构建 ReportArtifact 实体（status=GENERATED，createdAt 留空由 DB 默认值填充）。
     */
    private static ReportArtifact buildArtifact(Long reportId, String artifactType, String objectKey) {
        return ReportArtifact.builder()
                .reportId(reportId)
                .artifactType(artifactType)
                .objectKey(objectKey)
                .status(ReportArtifact.STATUS_GENERATED)
                .build();
    }

    /**
     * 顺序上传所有产物到 MinIO；单文件失败标记 FAILED 但保留行（spec §8.4）。
     *
     * <p>返回的 artifacts 列表与输入一一对应，但 status 字段会反映实际上传结果。
     * 单文件失败用 {@code onErrorReturn(false)} 兜底，避免 {@code onErrorResume}
     * 返回 {@code Mono.empty()} 导致 chain 断开、后续文件不再上传。</p>
     */
    private Mono<List<ReportArtifact>> uploadToMinioWithFallback(
            Long reportId, ParsedArtifacts parsed, List<ReportArtifact> artifacts) {
        Mono<List<ReportArtifact>> chain = Mono.just(new ArrayList<>(artifacts));
        int pdfIndex = parsed.pdf() != null ? 0 : -1;
        int mdIndex = parsed.pdf() != null ? 1 : 0;
        int chartStartIndex = (parsed.pdf() != null ? 1 : 0) + (parsed.markdown() != null ? 1 : 0);

        // PDF 上传
        if (pdfIndex >= 0) {
            final int idx = pdfIndex;
            chain = chain.flatMap(list -> uploadOne(list.get(idx).getObjectKey(),
                    parsed.pdf(), "application/pdf")
                    .doOnError(e -> log.warn("[ReportArtifactWriter] PDF 上传失败 reportId={} key={}",
                            reportId, list.get(idx).getObjectKey(), e))
                    .onErrorReturn(false)
                    .map(success -> {
                        if (!success) {
                            list.set(idx, markFailed(list.get(idx)));
                        }
                        return list;
                    }));
        }

        // Markdown 上传（mdIndex 只在 markdown != null 时使用）
        if (parsed.markdown() != null) {
            final int idx = mdIndex;
            byte[] mdBytes = parsed.markdown().getBytes(StandardCharsets.UTF_8);
            chain = chain.flatMap(list -> uploadOne(list.get(idx).getObjectKey(),
                    mdBytes, "text/markdown; charset=utf-8")
                    .doOnError(e -> log.warn("[ReportArtifactWriter] Markdown 上传失败 reportId={} key={}",
                            reportId, list.get(idx).getObjectKey(), e))
                    .onErrorReturn(false)
                    .map(success -> {
                        if (!success) {
                            list.set(idx, markFailed(list.get(idx)));
                        }
                        return list;
                    }));
        }

        // Charts 顺序上传
        int chartIdx = 0;
        for (ParsedChart chart : parsed.charts()) {
            final int idx = chartStartIndex + chartIdx;
            final byte[] pngBytes = chart.pngBytes();
            chain = chain.flatMap(list -> uploadOne(list.get(idx).getObjectKey(),
                    pngBytes, "image/png")
                    .doOnError(e -> log.warn("[ReportArtifactWriter] Chart 上传失败 reportId={} key={} type={}",
                            reportId, list.get(idx).getObjectKey(), chart.chartType(), e))
                    .onErrorReturn(false)
                    .map(success -> {
                        if (!success) {
                            list.set(idx, markFailed(list.get(idx)));
                        }
                        return list;
                    }));
            chartIdx++;
        }

        return chain;
    }

    private static ReportArtifact markFailed(ReportArtifact artifact) {
        artifact.setStatus(ReportArtifact.STATUS_FAILED);
        return artifact;
    }

    /**
     * 上传单个对象到 MinIO；成功返回 {@code true}，失败抛出 RuntimeException。
     *
     * <p>失败路径不在这里返回 false，而是让异常冒泡到调用方的
     * {@code onErrorReturn(false)} 兜底，触发该产物的 {@code status=FAILED}。
     * 这样调用方可以在 {@code doOnError} 中带上 objectKey / chart_type 等上下文
     * 输出 warn 日志，便于排查。</p>
     *
     * <p>MinIO putObject 是阻塞 IO 调用，订阅在 {@link Schedulers#boundedElastic()}
     * 上避免阻塞 Reactor 事件循环（spec §12.2 性能禁忌）。</p>
     */
    private Mono<Boolean> uploadOne(String objectKey, byte[] content, String contentType) {
        return Mono.fromCallable(() -> {
            minioClient.putObject(PutObjectArgs.builder()
                    .bucket(BUCKET)
                    .object(objectKey)
                    .stream(new ByteArrayInputStream(content), content.length, -1)
                    .contentType(contentType)
                    .build());
            return true;
        }).subscribeOn(Schedulers.boundedElastic());
    }

    /**
     * 确保 bucket 存在（首次部署 / 测试环境无 init_minio.py 时自动创建）。
     * BucketExistsArgs 抛异常时返回 empty 让流程继续 — MinIO 不可用时
     * 上传会失败，由 {@link #uploadOne} 的 onErrorResume 兜底。
     */
    private Mono<Void> ensureBucketExists() {
        return Mono.fromCallable(() -> {
            boolean exists = minioClient.bucketExists(BucketExistsArgs.builder().bucket(BUCKET).build());
            if (!exists) {
                try {
                    minioClient.makeBucket(MakeBucketArgs.builder().bucket(BUCKET).build());
                } catch (ErrorResponseException error) {
                    if (error.errorResponse() == null
                            || !"BucketAlreadyOwnedByYou".equals(error.errorResponse().code())) {
                        throw error;
                    }
                }
            }
            return true;
        }).subscribeOn(Schedulers.boundedElastic()).then();
    }

    /**
     * 在事务内顺序写入所有 report_artifact 行。
     *
     * <p>幂等检查：对每行先查 (reportId, artifactType) 是否已有 GENERATED 行；
     * 已有则跳过（避免重放时 MinIO 重复上传 + 表数据膨胀）。FAILED 行允许覆盖
     * （重试时可能从 FAILED → GENERATED）。</p>
     */
    private Mono<Integer> persistArtifacts(List<ReportArtifact> artifacts) {
        return Flux.fromIterable(artifacts)
                .concatMap(this::persistOneIfExistsOrFailed)
                .reduce(0, Integer::sum)
                .as(transactionalOperator::transactional);
    }

    private Mono<Integer> persistOneIfExistsOrFailed(ReportArtifact artifact) {
        return artifactRepo.findByReportIdAndArtifactType(artifact.getReportId(), artifact.getArtifactType())
                .flatMap(existing -> {
                    if (ReportArtifact.STATUS_GENERATED.equals(existing.getStatus())) {
                        log.info("[ReportArtifactWriter] 重放幂等命中，跳过 reportId={} type={}",
                                artifact.getReportId(), artifact.getArtifactType());
                        return Mono.just(0);
                    }
                    // FAILED 行覆盖为最新状态（重试场景）
                    existing.setObjectKey(artifact.getObjectKey());
                    existing.setStatus(artifact.getStatus());
                    return artifactRepo.save(existing).thenReturn(1);
                })
                .switchIfEmpty(Mono.defer(() -> artifactRepo.save(artifact).thenReturn(1)));
    }

    // ========================================================================
    // Helpers
    // ========================================================================

    /**
     * 解析 reportId：优先用 task.ref_report_id，缺失时回退到 report.task_id 关联查询。
     * 与 {@code CheckResultWriter.resolveReportId} 一致。
     */
    private Mono<Long> resolveReportId(String taskId) {
        return taskRepo.findById(taskId)
                .flatMap(task -> {
                    if (task.getRefReportId() != null) {
                        return Mono.just(task.getRefReportId());
                    }
                    return reportRepo.findByTaskId(taskId)
                            .map(Report::getId)
                            .switchIfEmpty(Mono.<Long>empty());
                })
                .switchIfEmpty(Mono.<Long>empty());
    }

    /**
     * spec §5.3 — {@code reports/{reportId}/report.pdf}。
     */
    static String buildPdfObjectKey(Long reportId) {
        return String.format("reports/%d/report.pdf", reportId);
    }

    /**
     * spec §5.3 — {@code reports/{reportId}/report.md}。
     */
    static String buildMarkdownObjectKey(Long reportId) {
        return String.format("reports/%d/report.md", reportId);
    }

    /**
     * spec §5.3 — {@code reports/{reportId}/charts/{filename}}。
     */
    static String buildChartObjectKey(Long reportId, String filename) {
        return String.format("reports/%d/charts/%s", reportId, filename);
    }

    // ========================================================================
    // Internal containers
    // ========================================================================

    /**
     * 内部解析结果容器（不可变）。
     *
     * @param pdf      PDF 字节（可空 — pdf_b64 缺失时）
     * @param markdown Markdown 文本（可空 — markdown 字段缺失时）
     * @param charts   解析出的图表列表（可空 — charts 字段缺失时为空列表）
     */
    record ParsedArtifacts(byte[] pdf, String markdown, List<ParsedChart> charts) {
    }

    /**
     * 单张图表解析结果。
     *
     * @param chartType L3 ChartType 枚举值（ASSET_STRUCTURE_PIE 等）
     * @param title      图表标题（M3.06 ChartResult.title）
     * @param pngBytes   PNG 字节
     */
    record ParsedChart(String chartType, String title, byte[] pngBytes) {
    }
}
