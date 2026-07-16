package com.finreport.service.file;

import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.util.concurrent.atomic.AtomicLong;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.core.io.buffer.DataBufferUtils;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.http.HttpStatus;
import org.springframework.http.codec.multipart.FilePart;
import org.springframework.stereotype.Service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.finreport.domain.dto.UploadResponse;
import com.finreport.domain.entity.Report;
import com.finreport.domain.entity.Task;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.exception.BusinessException;
import com.finreport.repository.ReportRepository;
import com.finreport.service.orchestrator.TaskOrchestrator;

import io.minio.BucketExistsArgs;
import io.minio.MakeBucketArgs;
import io.minio.MinioClient;
import io.minio.PutObjectArgs;
import io.minio.errors.ErrorResponseException;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/**
 * 文件服务 — PDF 上传到 MinIO，创建 report + task 记录。
 *
 * <p>负责 multipart 文件接收、MD5 去重、MinIO 存储、幂等性检查和任务创建。
 * Bucket 名称来自 spec §5.5.1，Object Key 规范来自 spec §5.5.2。</p>
 */
@Service
public class FileService {

    private static final Logger log = LoggerFactory.getLogger(FileService.class);

    private static final String BUCKET = "finreport-uploads";
    private static final long MAX_FILE_SIZE = 50 * 1024 * 1024; // 50 MB
    private static final String ALLOWED_CONTENT_TYPE = "application/pdf";
    private static final Duration IDEMPOTENCY_TTL = Duration.ofHours(24);
    private static final String IDEMPOTENCY_PREFIX = "fin:idem:upload:";
    private static final String DEFAULT_PARSE_STATUS = "PENDING";
    private static final int MD5_HEX_LENGTH = 32;

    private final MinioClient minioClient;
    private final ReportRepository reportRepo;
    private final TaskOrchestrator orchestrator;
    private final ReactiveRedisTemplate<String, String> redisTemplate;
    private final ObjectMapper objectMapper;

    public FileService(
            MinioClient minioClient,
            ReportRepository reportRepo,
            TaskOrchestrator orchestrator,
            ReactiveRedisTemplate<String, String> redisTemplate) {
        this.minioClient = minioClient;
        this.reportRepo = reportRepo;
        this.orchestrator = orchestrator;
        this.redisTemplate = redisTemplate;
        this.objectMapper = new ObjectMapper();
    }

    /**
     * 上传 PDF 并创建 report + task 记录。
     *
     * <p>处理流程：
     * <ol>
     *   <li>校验文件类型和大小</li>
     *   <li>计算文件 MD5</li>
     *   <li>幂等性检查（Idempotency-Key）</li>
     *   <li>MD5 去重（同一文件已存在则复用 report，创建新 task）</li>
     *   <li>上传到 MinIO</li>
     *   <li>创建 report 记录</li>
     *   <li>创建 task 并关联</li>
     *   <li>缓存幂等结果</li>
     * </ol>
     *
     * @param filePart      上传的 PDF 文件
     * @param companyCode   公司代码（如 600519）
     * @param companyName   公司名称（如 贵州茅台）
     * @param reportType    报告类型（如 ANNUAL）
     * @param reportPeriod  报告期间（如 2024-12-31）
     * @param userId        用户 ID
     * @param idempotencyKey 幂等键（可为 null）
     * @return UploadResponse（taskId + reportId + status）
     */
    public Mono<UploadResponse> upload(
            FilePart filePart,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            Long userId,
            String idempotencyKey) {

        log.debug("[FileService] upload 开始 userId={} companyCode={} filename={}",
                userId, companyCode, filePart.filename());

        // 1. 校验文件类型（放入 reactive chain 以便测试）
        return Mono.fromCallable(() -> {
            validateContentType(filePart);
            return true;
        })
                .then(Mono.defer(() -> {
                    // 2. 幂等性检查
                    if (idempotencyKey != null && !idempotencyKey.isBlank()) {
                        return checkIdempotency(idempotencyKey)
                                .switchIfEmpty(Mono.defer(() ->
                                        processUpload(filePart, companyCode, companyName,
                                                reportType, reportPeriod, userId, idempotencyKey)));
                    }
                    return processUpload(filePart, companyCode, companyName,
                            reportType, reportPeriod, userId, null);
                }));
    }

    // ========================================================================
    // Private helpers
    // ========================================================================

    /**
     * 核心上传流程：读取文件 → MD5 → 去重 → MinIO → 入库 → 创建 task。
     */
    private Mono<UploadResponse> processUpload(
            FilePart filePart,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            Long userId,
            String idempotencyKey) {

        return Mono.usingWhen(
                writeToTemporaryFile(filePart),
                temporaryFile -> computeMd5(temporaryFile.path())
                        .flatMap(md5 -> {
                            String safeFilename = sanitizeFilename(filePart.filename());

                            // MD5 去重：同一文件已存在则复用 report
                            return reportRepo.findByPdfMd5(md5)
                                    .flatMap(existingReport -> {
                                        log.info("[FileService] MD5 去重命中 reportId={} md5={}",
                                                existingReport.getId(), md5);
                                        return createTaskForReport(existingReport, userId,
                                                safeFilename, companyCode, companyName,
                                                reportType, reportPeriod, idempotencyKey);
                                    })
                                    .switchIfEmpty(Mono.defer(() ->
                                            uploadNewFile(temporaryFile, safeFilename, md5,
                                                    companyCode, companyName, reportType,
                                                    reportPeriod, userId, idempotencyKey)));
                        }),
                this::deleteTemporaryFile,
                (temporaryFile, error) -> deleteTemporaryFile(temporaryFile),
                this::deleteTemporaryFile);
    }

    /**
     * 上传新文件到 MinIO 并创建 report + task。
     *
     * <p>流程：创建 task（不调度）→ 上传 MinIO → 创建 report →
     * 更新 refReportId → 调度 PARSE（确保 L3 能下载到 PDF）。</p>
     */
    private Mono<UploadResponse> uploadNewFile(
            TemporaryUpload temporaryFile, String safeFilename, String md5,
            String companyCode, String companyName, String reportType,
            String reportPeriod, Long userId, String idempotencyKey) {

        // 先创建 task（不调度 — PARSE 需等 MinIO 上传完成）
        return orchestrator.createTaskWithoutDispatch(userId, null, buildPayload(
                        safeFilename, companyCode, companyName, reportType, reportPeriod))
                .flatMap(task -> {
                    String objectKey = buildObjectKey(userId, task.getId(), safeFilename);

                    return ensureBucketExists()
                            .then(uploadToMinio(temporaryFile, objectKey))
                            .then(createReport(task, userId, companyCode, companyName,
                                    reportType, reportPeriod, md5, objectKey))
                            .flatMap(report -> {
                                // 更新 task 的 refReportId
                                return orchestrator.updateRefReportId(task.getId(), report.getId())
                                        .thenReturn(new UploadResponse(
                                                task.getId(),
                                                report.getId(),
                                                TaskStatus.PENDING.name()));
                            })
                            .flatMap(response ->
                                    // MinIO 上传完成后调度 PARSE，附带 pdfObjectKey
                                    orchestrator.dispatchTask(task.getId(),
                                            java.util.Map.of("pdfObjectKey", objectKey))
                                            .then(cacheIdempotency(idempotencyKey, response))
                                            .thenReturn(response))
                            .onErrorResume(e -> {
                                // MinIO/DB 失败后标记 task 为 FAILED
                                log.error("[FileService] 上传流程失败 taskId={} error={}",
                                        task.getId(), e.getMessage());
                                return orchestrator.markTaskFailed(
                                                task.getId(),
                                                "上传失败: " + e.getMessage())
                                        .then(Mono.<UploadResponse>error(e));
                            });
                });
    }

    /**
     * 为已有 report 创建新的 task（MD5 去重场景）。
     */
    private Mono<UploadResponse> createTaskForReport(
            Report report, Long userId, String safeFilename,
            String companyCode, String companyName, String reportType,
            String reportPeriod, String idempotencyKey) {

        return orchestrator.createTask(userId, report.getId(), buildPayload(
                        safeFilename, companyCode, companyName, reportType, reportPeriod))
                .flatMap(task -> {
                    UploadResponse response = new UploadResponse(
                            task.getId(),
                            report.getId(),
                            TaskStatus.PENDING.name());
                    return cacheIdempotency(idempotencyKey, response)
                            .thenReturn(response);
                });
    }

    /**
     * 创建 report 记录（写入 MySQL）。
     */
    private Mono<Report> createReport(
            Task task, Long userId, String companyCode, String companyName,
            String reportType, String reportPeriod, String md5,
            String objectKey) {

        Report report = Report.builder()
                .taskId(task.getId())
                .userId(userId)
                .companyCode(companyCode)
                .companyName(companyName)
                .reportType(reportType)
                .reportPeriod(reportPeriod)
                .pdfMd5(md5)
                .pdfObjectKey(objectKey)
                .parseStatus(DEFAULT_PARSE_STATUS)
                .createdAt(java.time.LocalDateTime.now())
                .build();

        log.info("[FileService] 创建 report companyCode={} reportPeriod={} md5={}",
                companyCode, reportPeriod, md5);

        return reportRepo.save(report);
    }

    /**
     * 上传文件到 MinIO。
     */
    private Mono<Void> uploadToMinio(TemporaryUpload temporaryFile, String objectKey) {
        return Mono.fromCallable(() -> {
            try (InputStream inputStream = Files.newInputStream(temporaryFile.path())) {
                minioClient.putObject(PutObjectArgs.builder()
                        .bucket(BUCKET)
                        .object(objectKey)
                        .stream(inputStream, temporaryFile.size(), -1)
                        .contentType(ALLOWED_CONTENT_TYPE)
                        .build());
                log.info("[FileService] MinIO 上传完成 objectKey={} size={}",
                        objectKey, temporaryFile.size());
                return true;
            }
        }).subscribeOn(Schedulers.boundedElastic()).then();
    }

    /**
     * 确保 MinIO bucket 存在，不存在则创建。
     * 处理并发创建导致的 BucketAlreadyOwnedByYou 异常。
     */
    private Mono<Void> ensureBucketExists() {
        return Mono.fromCallable(() -> {
            boolean exists = minioClient.bucketExists(
                    BucketExistsArgs.builder().bucket(BUCKET).build());
            if (!exists) {
                try {
                    minioClient.makeBucket(MakeBucketArgs.builder().bucket(BUCKET).build());
                    log.info("[FileService] 创建 MinIO bucket: {}", BUCKET);
                } catch (ErrorResponseException e) {
                    // 并发场景：其他请求已创建 bucket，忽略
                    if (e.errorResponse() != null
                            && "BucketAlreadyOwnedByYou".equals(e.errorResponse().code())) {
                        log.debug("[FileService] Bucket 已被其他请求创建: {}", BUCKET);
                    } else {
                        throw e;
                    }
                }
            }
            return true;
        }).subscribeOn(Schedulers.boundedElastic()).then();
    }

    /**
     * 缓存幂等结果到 Redis（失败不影响主流程）。
     */
    private Mono<Boolean> cacheIdempotency(String idempotencyKey, UploadResponse response) {
        if (idempotencyKey == null || idempotencyKey.isBlank()) {
            return Mono.just(false);
        }
        String key = IDEMPOTENCY_PREFIX + idempotencyKey;
        try {
            String value = objectMapper.writeValueAsString(response);
            return redisTemplate.opsForValue()
                    .set(key, value, IDEMPOTENCY_TTL)
                    .doOnSuccess(ok -> log.debug("[FileService] 幂等缓存 key={}", idempotencyKey))
                    .onErrorResume(e -> {
                        log.warn("[FileService] 幂等缓存失败（Redis 不可用）key={}", idempotencyKey, e);
                        return Mono.just(false);
                    });
        } catch (Exception e) {
            log.warn("[FileService] 幂等缓存序列化失败 key={}", idempotencyKey, e);
            return Mono.just(false);
        }
    }

    /**
     * 检查幂等：若 key 已存在则返回缓存结果，Redis 不可用时降级放行。
     */
    private Mono<UploadResponse> checkIdempotency(String idempotencyKey) {
        String key = IDEMPOTENCY_PREFIX + idempotencyKey;
        return redisTemplate.opsForValue()
                .get(key)
                .flatMap(cached -> {
                    try {
                        UploadResponse response = objectMapper.readValue(cached, UploadResponse.class);
                        log.info("[FileService] 幂等命中 key={} taskId={}", idempotencyKey, response.taskId());
                        return Mono.just(response);
                    } catch (Exception e) {
                        log.warn("[FileService] 幂等缓存反序列化失败 key={}", idempotencyKey, e);
                        return Mono.empty();
                    }
                })
                .onErrorResume(e -> {
                    log.warn("[FileService] Redis 不可用，幂等检查降级放行 key={}", idempotencyKey, e);
                    return Mono.empty();
                });
    }

    // ========================================================================
    // Static helpers
    // ========================================================================

    /**
     * 将 multipart 内容流式写入临时文件，并在写入时实施 50MB 限制。
     */
    private Mono<TemporaryUpload> writeToTemporaryFile(FilePart filePart) {
        AtomicLong byteCount = new AtomicLong();
        return Mono.fromCallable(() -> Files.createTempFile("finreport-upload-", ".pdf"))
                .subscribeOn(Schedulers.boundedElastic())
                .flatMap(path -> DataBufferUtils.write(
                                filePart.content().<DataBuffer>handle((buffer, sink) -> {
                                    long totalSize = byteCount.addAndGet(buffer.readableByteCount());
                                    if (totalSize > MAX_FILE_SIZE) {
                                        DataBufferUtils.release(buffer);
                                        sink.error(new BusinessException(
                                                HttpStatus.PAYLOAD_TOO_LARGE,
                                                "FILE_TOO_LARGE",
                                                "PDF 超过 50MB 限制"));
                                        return;
                                    }
                                    sink.next(buffer);
                                })
                                        .doOnDiscard(DataBuffer.class, DataBufferUtils::release),
                                path,
                                StandardOpenOption.TRUNCATE_EXISTING)
                        .then(Mono.fromSupplier(() -> new TemporaryUpload(path, byteCount.get())))
                        .onErrorResume(error -> deleteTemporaryFile(new TemporaryUpload(path, 0))
                                .then(Mono.error(error))));
    }

    /**
     * 在 boundedElastic 调度器中以固定大小缓冲区计算临时文件 MD5。
     */
    private Mono<String> computeMd5(Path path) {
        return Mono.fromCallable(() -> {
            MessageDigest digest = MessageDigest.getInstance("MD5");
            try (InputStream inputStream = Files.newInputStream(path)) {
                byte[] buffer = new byte[8192];
                int bytesRead;
                while ((bytesRead = inputStream.read(buffer)) != -1) {
                    digest.update(buffer, 0, bytesRead);
                }
            }
            return toHex(digest.digest());
        }).subscribeOn(Schedulers.boundedElastic())
                .onErrorMap(error -> new BusinessException(
                        HttpStatus.INTERNAL_SERVER_ERROR,
                        "MD5_COMPUTE_FAILED",
                        "MD5 计算失败",
                        error));
    }

    /**
     * 删除临时上传文件，删除失败只记录日志。
     */
    private Mono<Void> deleteTemporaryFile(TemporaryUpload temporaryFile) {
        return Mono.fromRunnable(() -> {
            try {
                Files.deleteIfExists(temporaryFile.path());
            } catch (java.io.IOException error) {
                log.warn("[FileService] 删除临时文件失败 path={}", temporaryFile.path(), error);
            }
        }).subscribeOn(Schedulers.boundedElastic()).then();
    }

    /**
     * 计算文件 MD5（十六进制字符串）。
     */
    static String computeMd5(byte[] data) {
        try {
            MessageDigest md = MessageDigest.getInstance("MD5");
            return toHex(md.digest(data));
        } catch (Exception e) {
            throw new BusinessException(
                    HttpStatus.INTERNAL_SERVER_ERROR,
                    "MD5_COMPUTE_FAILED",
                    "MD5 计算失败",
                    e);
        }
    }

    private static String toHex(byte[] digest) {
        StringBuilder hex = new StringBuilder(MD5_HEX_LENGTH);
        for (byte value : digest) {
            hex.append(String.format("%02x", value));
        }
        return hex.toString();
    }

    private record TemporaryUpload(Path path, long size) {
    }

    /**
     * 校验文件 Content-Type 为 application/pdf。
     * 使用 isCompatibleWith 支持带参数的 MIME（如 application/pdf; charset=binary）。
     */
    private static void validateContentType(FilePart filePart) {
        org.springframework.http.MediaType mediaType = filePart.headers().getContentType();
        if (mediaType == null || !mediaType.isCompatibleWith(
                org.springframework.http.MediaType.APPLICATION_PDF)) {
            throw new BusinessException(
                    HttpStatus.UNSUPPORTED_MEDIA_TYPE,
                    "INVALID_FILE_TYPE",
                    "仅支持 PDF 文件上传，收到: " + (mediaType != null ? mediaType : "unknown"));
        }
    }

    /**
     * 构建 MinIO object key（spec §5.5.2）。
     */
    static String buildObjectKey(Long userId, String taskId, String filename) {
        String yyyyMM = LocalDate.now().format(DateTimeFormatter.ofPattern("yyyyMM"));
        return String.format("uploads/%d/%s/%s/%s", userId, yyyyMM, taskId, filename);
    }

    /**
     * 构建任务 payload（JSON）。
     */
    private static java.util.Map<String, Object> buildPayload(
            String filename, String companyCode, String companyName,
            String reportType, String reportPeriod) {
        java.util.Map<String, Object> payload = new java.util.LinkedHashMap<>();
        payload.put("filename", filename);
        payload.put("companyCode", companyCode);
        payload.put("companyName", companyName);
        payload.put("reportType", reportType);
        payload.put("reportPeriod", reportPeriod);
        return payload;
    }

    /**
     * 清理文件名，移除路径分隔符和特殊字符。
     */
    private static String sanitizeFilename(String filename) {
        if (filename == null) return "report.pdf";
        return filename.replaceAll("[\\\\/:*?\"<>|]", "_").trim();
    }
}
