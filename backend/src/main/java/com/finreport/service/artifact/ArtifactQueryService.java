package com.finreport.service.artifact;

import java.time.ZoneOffset;
import java.util.concurrent.TimeUnit;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

import com.finreport.domain.dto.ReportArtifactResponse;
import com.finreport.domain.entity.Report;
import com.finreport.domain.entity.ReportArtifact;
import com.finreport.exception.BusinessException;
import com.finreport.repository.ReportArtifactRepository;
import com.finreport.repository.ReportRepository;

import io.minio.GetPresignedObjectUrlArgs;
import io.minio.MinioClient;
import io.minio.http.Method;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/**
 * 报告产物查询服务 — spec §6.2.2 / M3.09。
 *
 * <p>按 reportId 查询 {@code report_artifact} 表，为每个 GENERATED 产物生成
 * MinIO 预签名下载 URL；FAILED 产物返回空字符串。先做 report 归属校验
 * （spec §8.5 用户隔离）。</p>
 */
@Service
public class ArtifactQueryService {

    private static final Logger log = LoggerFactory.getLogger(ArtifactQueryService.class);

    /**
     * 预签名 URL 有效期 — 默认 1 小时，满足一次性下载需求。
     */
    private static final int PRESIGNED_URL_EXPIRY_HOURS = 1;

    private final ReportRepository reportRepo;
    private final ReportArtifactRepository artifactRepo;
    private final MinioClient minioClient;

    public ArtifactQueryService(
            ReportRepository reportRepo,
            ReportArtifactRepository artifactRepo,
            MinioClient minioClient) {
        this.reportRepo = reportRepo;
        this.artifactRepo = artifactRepo;
        this.minioClient = minioClient;
    }

    /**
     * 查询某份报告的所有产物，附 MinIO 预签名下载 URL。
     *
     * @param reportId 报告 ID
     * @param userId   当前用户 ID
     * @return 产物列表 Flux；report 不存在或无权限抛 {@code REPORT_NOT_FOUND}
     */
    public Flux<ReportArtifactResponse> listArtifacts(Long reportId, Long userId) {
        log.debug("[ArtifactQueryService] listArtifacts reportId={} userId={}", reportId, userId);
        return assertReportOwnership(reportId, userId)
                .thenMany(artifactRepo.findByReportIdOrderByArtifactTypeAsc(reportId)
                        .concatMap(artifact -> enrichWithUrl(artifact)
                                .subscribeOn(Schedulers.boundedElastic())));
    }

    private Mono<ReportArtifactResponse> enrichWithUrl(ReportArtifact artifact) {
        // FAILED 产物直接返回空 URL，无需调用 MinIO。
        if (!ReportArtifact.STATUS_GENERATED.equals(artifact.getStatus())) {
            return Mono.just(toResponse(artifact, ""));
        }
        // 用 fromCallable 把阻塞调用延后到订阅时执行，配合外层
        // subscribeOn(boundedElastic) 真正切到 worker 线程；如果直接在
        // concatMap lambda 里同步调用 getPresignedObjectUrl，subscribeOn
        // 此时还未生效（Mono.just 已捕获值），阻塞会落在 Reactor 事件循环
        // 上违反 spec §12.2 性能禁忌。
        return Mono.fromCallable(() -> {
            try {
                String url = minioClient.getPresignedObjectUrl(GetPresignedObjectUrlArgs.builder()
                        .method(Method.GET)
                        .bucket(ReportArtifactWriter.BUCKET)
                        .object(artifact.getObjectKey())
                        .expiry(PRESIGNED_URL_EXPIRY_HOURS, TimeUnit.HOURS)
                        .build());
                return toResponse(artifact, url);
            } catch (Exception e) {
                log.warn("[ArtifactQueryService] 预签名 URL 生成失败 reportId={} key={}",
                        artifact.getReportId(), artifact.getObjectKey(), e);
                return toResponse(artifact, "");
            }
        });
    }

    private Mono<Report> assertReportOwnership(Long reportId, Long userId) {
        return reportRepo.findById(reportId)
                .filter(report -> userId.equals(report.getUserId()))
                .switchIfEmpty(Mono.error(new BusinessException(
                        HttpStatus.NOT_FOUND, "REPORT_NOT_FOUND",
                        "报告不存在: " + reportId)));
    }

    private static ReportArtifactResponse toResponse(ReportArtifact artifact, String downloadUrl) {
        return new ReportArtifactResponse(
                artifact.getId(),
                artifact.getArtifactType(),
                artifact.getObjectKey(),
                artifact.getStatus(),
                downloadUrl,
                artifact.getCreatedAt() != null
                        ? artifact.getCreatedAt().atZone(ZoneOffset.UTC).toInstant()
                        : null);
    }
}
