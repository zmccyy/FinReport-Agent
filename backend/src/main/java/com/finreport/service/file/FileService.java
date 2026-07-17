package com.finreport.service.file;

import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.util.LinkedHashMap;
import java.util.Map;
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
 * <p>上传先流式落盘并计算内容 MD5，再处理幂等声明和同用户内容去重。这样同一
 * Idempotency-Key 既可检测不同文件的误用，也不会出现并发上传时的先查再写竞态。</p>
 */
@Service
public class FileService {

    private static final Logger log = LoggerFactory.getLogger(FileService.class);

    private static final String BUCKET = "finreport-uploads";
    private static final long MAX_FILE_SIZE = 50 * 1024 * 1024;
    private static final String ALLOWED_CONTENT_TYPE = "application/pdf";
    private static final Duration IDEMPOTENCY_TTL = Duration.ofHours(24);
    private static final String IDEMPOTENCY_PREFIX = "fin:idem:upload:";
    private static final String IDEMPOTENCY_PROCESSING = "PROCESSING";
    private static final String IDEMPOTENCY_COMPLETED = "COMPLETED";
    private static final String DEFAULT_PARSE_STATUS = "PENDING";
    private static final int MD5_HEX_LENGTH = 32;
    private static final int IDEMPOTENCY_POLL_ATTEMPTS = 3;
    private static final Duration IDEMPOTENCY_POLL_DELAY = Duration.ofMillis(100);

    private final MinioClient minioClient;
    private final ReportRepository reportRepo;
    private final TaskOrchestrator orchestrator;
    private final ReactiveRedisTemplate<String, String> redisTemplate;
    private final ObjectMapper objectMapper;

    /**
     * 构造文件服务。
     *
     * @param minioClient MinIO 客户端
     * @param reportRepo 报告仓库
     * @param orchestrator 任务编排器
     * @param redisTemplate Redis 客户端
     */
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
     * 上传 PDF 并创建或复用 report + task。
     *
     * @param filePart 上传的 PDF 文件
     * @param companyCode 公司代码
     * @param companyName 公司名称
     * @param reportType 报告类型
     * @param reportPeriod 报告期间
     * @param userId 当前用户 ID
     * @param idempotencyKey 请求幂等键（可空）
     * @return 上传或复用的任务响应
     */
    public Mono<UploadResponse> upload(
            FilePart filePart,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            Long userId,
            String idempotencyKey) {
        log.debug("[FileService] upload userId={} companyCode={} filename={}",
                userId, companyCode, filePart.filename());

        return Mono.fromRunnable(() -> validateContentType(filePart))
                .then(Mono.usingWhen(
                        writeToTemporaryFile(filePart),
                        temporary -> computeMd5(temporary.path()).flatMap(md5 -> processUpload(
                                temporary,
                                sanitizeFilename(filePart.filename()),
                                md5,
                                companyCode,
                                companyName,
                                reportType,
                                reportPeriod,
                                userId,
                                normalizeIdempotencyKey(idempotencyKey))),
                        this::deleteTemporaryFile,
                        (temporary, error) -> deleteTemporaryFile(temporary),
                        this::deleteTemporaryFile));
    }

    private Mono<UploadResponse> processUpload(
            TemporaryUpload temporary,
            String filename,
            String md5,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            Long userId,
            String idempotencyKey) {
        if (idempotencyKey == null) {
            return createOrReuseReport(
                    temporary, filename, md5, companyCode, companyName, reportType, reportPeriod, userId, null);
        }

        String redisKey = idempotencyRedisKey(userId, idempotencyKey);
        return getIdempotencyRecord(redisKey)
                .flatMap(record -> resolveExistingIdempotency(
                        redisKey, record, md5, temporary, filename, companyCode, companyName,
                        reportType, reportPeriod, userId, idempotencyKey))
                .switchIfEmpty(Mono.defer(() -> claimAndProcess(
                        redisKey, temporary, filename, md5, companyCode, companyName,
                        reportType, reportPeriod, userId, idempotencyKey)));
    }

    private Mono<UploadResponse> resolveExistingIdempotency(
            String redisKey,
            IdempotencyRecord record,
            String md5,
            TemporaryUpload temporary,
            String filename,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            Long userId,
            String idempotencyKey) {
        if (record.md5() != null && !record.md5().equals(md5)) {
            return Mono.error(new BusinessException(
                    HttpStatus.CONFLICT,
                    "IDEMPOTENCY_KEY_REUSED",
                    "同一个 Idempotency-Key 不能用于不同的文件"));
        }
        if (IDEMPOTENCY_COMPLETED.equals(record.state()) && record.response() != null) {
            log.info("[FileService] 幂等命中 userId={} key={} taskId={}",
                    userId, idempotencyKey, record.response().taskId());
            return Mono.just(record.response());
        }
        return waitForCompletedIdempotency(redisKey, md5, IDEMPOTENCY_POLL_ATTEMPTS)
                .switchIfEmpty(Mono.error(new BusinessException(
                        HttpStatus.CONFLICT,
                        "IDEMPOTENCY_REQUEST_IN_PROGRESS",
                        "同一 Idempotency-Key 的上传请求仍在处理中")));
    }

    private Mono<UploadResponse> claimAndProcess(
            String redisKey,
            TemporaryUpload temporary,
            String filename,
            String md5,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            Long userId,
            String idempotencyKey) {
        return setIdempotencyRecord(redisKey, new IdempotencyRecord(IDEMPOTENCY_PROCESSING, md5, null), true)
                .flatMap(claimed -> {
                    if (Boolean.TRUE.equals(claimed)) {
                        return createOrReuseReport(
                                temporary, filename, md5, companyCode, companyName, reportType, reportPeriod,
                                userId, idempotencyKey)
                                .flatMap(response -> cacheCompletedIdempotency(redisKey, md5, response)
                                        .thenReturn(response))
                                .onErrorResume(error -> clearProcessingIdempotency(redisKey).then(Mono.error(error)));
                    }
                    return getIdempotencyRecord(redisKey)
                            .flatMap(record -> resolveExistingIdempotency(
                                    redisKey, record, md5, temporary, filename, companyCode, companyName,
                                    reportType, reportPeriod, userId, idempotencyKey))
                            .switchIfEmpty(Mono.error(new BusinessException(
                                    HttpStatus.CONFLICT,
                                    "IDEMPOTENCY_REQUEST_IN_PROGRESS",
                                    "同一 Idempotency-Key 的上传请求仍在处理中")));
                })
                .onErrorResume(error -> {
                    if (error instanceof BusinessException) {
                        return Mono.error(error);
                    }
                    log.warn("[FileService] Redis 幂等声明失败，使用数据库唯一约束兜底 key={}", redisKey, error);
                    return createOrReuseReport(
                            temporary, filename, md5, companyCode, companyName, reportType, reportPeriod,
                            userId, idempotencyKey);
                });
    }

    private Mono<UploadResponse> waitForCompletedIdempotency(String redisKey, String md5, int attemptsRemaining) {
        if (attemptsRemaining <= 0) {
            return Mono.empty();
        }
        return Mono.delay(IDEMPOTENCY_POLL_DELAY)
                .then(getIdempotencyRecord(redisKey))
                .flatMap(record -> {
                    if (record.md5() != null && !record.md5().equals(md5)) {
                        return Mono.error(new BusinessException(
                                HttpStatus.CONFLICT,
                                "IDEMPOTENCY_KEY_REUSED",
                                "同一个 Idempotency-Key 不能用于不同的文件"));
                    }
                    if (IDEMPOTENCY_COMPLETED.equals(record.state()) && record.response() != null) {
                        return Mono.just(record.response());
                    }
                    return waitForCompletedIdempotency(redisKey, md5, attemptsRemaining - 1);
                })
                .switchIfEmpty(waitForCompletedIdempotency(redisKey, md5, attemptsRemaining - 1));
    }

    private Mono<UploadResponse> createOrReuseReport(
            TemporaryUpload temporary,
            String filename,
            String md5,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            Long userId,
            String idempotencyKey) {
        return findReportForUserAndMd5(userId, md5)
                .flatMap(report -> reuseExistingReport(
                        report, filename, companyCode, companyName, reportType, reportPeriod, userId,
                        idempotencyKey))
                .switchIfEmpty(Mono.defer(() -> uploadNewFile(
                        temporary, filename, md5, companyCode, companyName, reportType, reportPeriod, userId)));
    }

    private Mono<UploadResponse> reuseExistingReport(
            Report report,
            String filename,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            Long userId,
            String idempotencyKey) {
        if (report.getTaskId() == null || report.getTaskId().isBlank()) {
            return retryExistingReport(report, filename, companyCode, companyName, reportType, reportPeriod, userId,
                    idempotencyKey);
        }
        return orchestrator.findByIdAndUserId(report.getTaskId(), userId)
                .flatMap(task -> {
                    TaskStatus taskStatus = TaskStatus.valueOf(task.getStatus());
                    if (!taskStatus.isTerminal() || taskStatus == TaskStatus.COMPLETED) {
                        return Mono.just(new UploadResponse(task.getId(), report.getId(), task.getStatus()));
                    }
                    return retryExistingReport(
                            report, filename, companyCode, companyName, reportType, reportPeriod, userId,
                            idempotencyKey);
                })
                .switchIfEmpty(Mono.defer(() -> retryExistingReport(
                        report, filename, companyCode, companyName, reportType, reportPeriod, userId,
                        idempotencyKey)));
    }

    private Mono<UploadResponse> retryExistingReport(
            Report report,
            String filename,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            Long userId,
            String idempotencyKey) {
        if (idempotencyKey == null) {
            return Mono.error(new BusinessException(
                    HttpStatus.CONFLICT,
                    "IDEMPOTENCY_KEY_REQUIRED_FOR_RETRY",
                    "失败或取消的任务必须使用新的 Idempotency-Key 重试"));
        }
        Map<String, Object> payload = buildPayload(
                filename, companyCode, companyName, reportType, reportPeriod, report.getPdfObjectKey());
        return orchestrator.createTaskWithoutDispatch(userId, report.getId(), payload)
                .flatMap(task -> {
                    report.setTaskId(task.getId());
                    return reportRepo.save(report)
                            .then(orchestrator.dispatchTask(task.getId(), Map.of("pdfObjectKey", report.getPdfObjectKey())))
                            .thenReturn(new UploadResponse(task.getId(), report.getId(), TaskStatus.PENDING.name()));
                });
    }

    private Mono<UploadResponse> uploadNewFile(
            TemporaryUpload temporary,
            String filename,
            String md5,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            Long userId) {
        String objectKey = buildContentObjectKey(userId, md5, filename);
        Map<String, Object> payload = buildPayload(
                filename, companyCode, companyName, reportType, reportPeriod, objectKey);
        return orchestrator.createTaskWithoutDispatch(userId, null, payload)
                .flatMap(task -> ensureBucketExists()
                        .then(uploadToMinio(temporary, objectKey))
                        .then(createReport(task, userId, companyCode, companyName, reportType, reportPeriod, md5, objectKey))
                        .flatMap(report -> orchestrator.updateRefReportId(task.getId(), report.getId())
                                .then(orchestrator.dispatchTask(task.getId(), Map.of("pdfObjectKey", objectKey)))
                                .thenReturn(new UploadResponse(
                                        task.getId(), report.getId(), TaskStatus.PENDING.name())))
                        .onErrorResume(error -> recoverDuplicateOrFail(
                                task, userId, md5, filename, companyCode, companyName, reportType, reportPeriod,
                                error)));
    }

    private Mono<UploadResponse> recoverDuplicateOrFail(
            Task task,
            Long userId,
            String md5,
            String filename,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            Throwable error) {
        if (isDuplicateKey(error)) {
            log.info("[FileService] report 唯一键冲突，重新读取已有报告 userId={} md5={}", userId, md5);
            return orchestrator.markTaskFailed(task.getId(), "重复上传竞争导致未使用任务")
                    .then(findReportForUserAndMd5(userId, md5))
                    .flatMap(report -> reuseExistingReport(
                            report, filename, companyCode, companyName, reportType, reportPeriod, userId,
                            "database-duplicate-recovery"));
        }
        log.error("[FileService] 上传流程失败 taskId={} error={}", task.getId(), error.getMessage());
        return orchestrator.markTaskFailed(task.getId(), "上传失败: " + error.getMessage())
                .then(Mono.error(error));
    }

    private Mono<Report> findReportForUserAndMd5(Long userId, String md5) {
        Mono<Report> scoped = reportRepo.findByUserIdAndPdfMd5(userId, md5);
        if (scoped != null) {
            return scoped;
        }
        Mono<Report> legacy = reportRepo.findByPdfMd5(md5);
        return legacy == null ? Mono.empty() : legacy.filter(report -> userId.equals(report.getUserId()));
    }

    private Mono<Report> createReport(
            Task task,
            Long userId,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            String md5,
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
        return reportRepo.save(report);
    }

    private Mono<Void> uploadToMinio(TemporaryUpload temporary, String objectKey) {
        return Mono.fromCallable(() -> {
            try (InputStream inputStream = Files.newInputStream(temporary.path())) {
                minioClient.putObject(PutObjectArgs.builder()
                        .bucket(BUCKET)
                        .object(objectKey)
                        .stream(inputStream, temporary.size(), -1)
                        .contentType(ALLOWED_CONTENT_TYPE)
                        .build());
                return true;
            }
        }).subscribeOn(Schedulers.boundedElastic()).then();
    }

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

    private Mono<IdempotencyRecord> getIdempotencyRecord(String redisKey) {
        return redisTemplate.opsForValue().get(redisKey).flatMap(value -> {
            try {
                return Mono.just(objectMapper.readValue(value, IdempotencyRecord.class));
            } catch (Exception ignored) {
                try {
                    return Mono.just(new IdempotencyRecord(
                            IDEMPOTENCY_COMPLETED, null, objectMapper.readValue(value, UploadResponse.class)));
                } catch (Exception error) {
                    log.warn("[FileService] 无法读取幂等记录 key={}", redisKey, error);
                    return Mono.empty();
                }
            }
        });
    }

    private Mono<Boolean> setIdempotencyRecord(String redisKey, IdempotencyRecord record, boolean onlyIfAbsent) {
        return Mono.defer(() -> {
            try {
                String value = objectMapper.writeValueAsString(record);
                return onlyIfAbsent
                        ? redisTemplate.opsForValue().setIfAbsent(redisKey, value, IDEMPOTENCY_TTL)
                        : redisTemplate.opsForValue().set(redisKey, value, IDEMPOTENCY_TTL);
            } catch (Exception error) {
                return Mono.error(error);
            }
        });
    }

    private Mono<Void> cacheCompletedIdempotency(String redisKey, String md5, UploadResponse response) {
        return setIdempotencyRecord(redisKey, new IdempotencyRecord(IDEMPOTENCY_COMPLETED, md5, response), false)
                .onErrorResume(error -> {
                    log.warn("[FileService] 不能持久化幂等响应 key={}", redisKey, error);
                    return Mono.just(false);
                })
                .then();
    }

    private Mono<Void> clearProcessingIdempotency(String redisKey) {
        return Mono.defer(() -> redisTemplate.delete(redisKey)).onErrorResume(error -> {
            log.warn("[FileService] 不能清理幂等声明 key={}", redisKey, error);
            return Mono.just(0L);
        }).then();
    }

    private Mono<TemporaryUpload> writeToTemporaryFile(FilePart filePart) {
        AtomicLong byteCount = new AtomicLong();
        return Mono.fromCallable(() -> Files.createTempFile("finreport-upload-", ".pdf"))
                .subscribeOn(Schedulers.boundedElastic())
                .flatMap(path -> DataBufferUtils.write(
                        filePart.content().<DataBuffer>handle((buffer, sink) -> {
                            long total = byteCount.addAndGet(buffer.readableByteCount());
                            if (total > MAX_FILE_SIZE) {
                                DataBufferUtils.release(buffer);
                                sink.error(new BusinessException(
                                        HttpStatus.PAYLOAD_TOO_LARGE, "FILE_TOO_LARGE", "PDF 超过 50MB 限制"));
                                return;
                            }
                            sink.next(buffer);
                        }).doOnDiscard(DataBuffer.class, DataBufferUtils::release),
                        path,
                        StandardOpenOption.TRUNCATE_EXISTING)
                        .then(Mono.fromSupplier(() -> new TemporaryUpload(path, byteCount.get())))
                        .onErrorResume(error -> deleteTemporaryFile(new TemporaryUpload(path, 0)).then(Mono.error(error))));
    }

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
                        HttpStatus.INTERNAL_SERVER_ERROR, "MD5_COMPUTE_FAILED", "MD5 计算失败", error));
    }

    private Mono<Void> deleteTemporaryFile(TemporaryUpload temporary) {
        return Mono.fromRunnable(() -> {
            try {
                Files.deleteIfExists(temporary.path());
            } catch (java.io.IOException error) {
                log.warn("[FileService] 删除临时文件失败 path={}", temporary.path(), error);
            }
        }).subscribeOn(Schedulers.boundedElastic()).then();
    }

    /**
     * 计算字节数组的 MD5。
     *
     * @param data 输入字节
     * @return 小写十六进制 MD5
     */
    static String computeMd5(byte[] data) {
        try {
            return toHex(MessageDigest.getInstance("MD5").digest(data));
        } catch (Exception error) {
            throw new BusinessException(HttpStatus.INTERNAL_SERVER_ERROR, "MD5_COMPUTE_FAILED", "MD5 计算失败", error);
        }
    }

    private static String toHex(byte[] digest) {
        StringBuilder hex = new StringBuilder(MD5_HEX_LENGTH);
        for (byte value : digest) {
            hex.append(String.format("%02x", value));
        }
        return hex.toString();
    }

    private static void validateContentType(FilePart filePart) {
        org.springframework.http.MediaType mediaType = filePart.headers().getContentType();
        if (mediaType == null || !mediaType.isCompatibleWith(org.springframework.http.MediaType.APPLICATION_PDF)) {
            throw new BusinessException(
                    HttpStatus.UNSUPPORTED_MEDIA_TYPE,
                    "INVALID_FILE_TYPE",
                    "仅支持 PDF 文件上传，收到: " + (mediaType != null ? mediaType : "unknown"));
        }
    }

    /**
     * 构建旧版 task 维度 object key，保留给历史调用方。
     *
     * @param userId 用户 ID
     * @param taskId 任务 ID
     * @param filename 文件名
     * @return object key
     */
    static String buildObjectKey(Long userId, String taskId, String filename) {
        String yyyyMM = LocalDate.now().format(DateTimeFormatter.ofPattern("yyyyMM"));
        return String.format("uploads/%d/%s/%s/%s", userId, yyyyMM, taskId, filename);
    }

    /**
     * 构建用户隔离、内容寻址的 object key。
     *
     * @param userId 用户 ID
     * @param md5 文件 MD5
     * @param filename 清理后的文件名
     * @return stable object key
     */
    static String buildContentObjectKey(Long userId, String md5, String filename) {
        return String.format("uploads/%d/%s/%s", userId, md5, filename);
    }

    private static Map<String, Object> buildPayload(
            String filename,
            String companyCode,
            String companyName,
            String reportType,
            String reportPeriod,
            String pdfObjectKey) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("filename", filename);
        payload.put("companyCode", companyCode);
        payload.put("companyName", companyName);
        payload.put("reportType", reportType);
        payload.put("reportPeriod", reportPeriod);
        payload.put("pdfObjectKey", pdfObjectKey);
        return payload;
    }

    private static String normalizeIdempotencyKey(String idempotencyKey) {
        return idempotencyKey == null || idempotencyKey.isBlank() ? null : idempotencyKey.trim();
    }

    private static String idempotencyRedisKey(Long userId, String idempotencyKey) {
        return IDEMPOTENCY_PREFIX + userId + ":" + idempotencyKey;
    }

    private static boolean isDuplicateKey(Throwable error) {
        Throwable cursor = error;
        while (cursor != null) {
            String className = cursor.getClass().getSimpleName();
            String message = cursor.getMessage();
            if (className.contains("DuplicateKey")
                    || (message != null && (message.contains("Duplicate entry") || message.contains("uk_report_user_md5")))) {
                return true;
            }
            cursor = cursor.getCause();
        }
        return false;
    }

    private static String sanitizeFilename(String filename) {
        if (filename == null || filename.isBlank()) {
            return "report.pdf";
        }
        return filename.replaceAll("[\\\\/:*?\"<>|]", "_").trim();
    }

    private record TemporaryUpload(Path path, long size) {
    }

    private record IdempotencyRecord(String state, String md5, UploadResponse response) {
    }
}
