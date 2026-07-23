package com.finreport.service.reasoner;

import java.time.ZoneOffset;
import java.util.Comparator;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

import com.finreport.domain.dto.AnomalyResponse;
import com.finreport.domain.entity.AnomalyRecord;
import com.finreport.domain.entity.Report;
import com.finreport.exception.BusinessException;
import com.finreport.repository.AnomalyRepository;
import com.finreport.repository.ReportRepository;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 异常检测结果查询服务 — spec §6.2.2 / M3.09。
 *
 * <p>按 reportId 查询 {@code anomaly} 表；由于 R2DBC derived query 不方便做
 * CASE WHEN 自定义排序，查询后按严重度权重排序（CRITICAL > ERROR > WARN > INFO）。
 * 先做 report 归属校验（spec §8.5 用户隔离）。</p>
 */
@Service
public class AnomalyQueryService {

    private static final Logger log = LoggerFactory.getLogger(AnomalyQueryService.class);

    private static final Map<String, Integer> SEVERITY_ORDER = Map.of(
            "CRITICAL", 0,
            "ERROR", 1,
            "WARN", 2,
            "INFO", 3);

    private static final Comparator<AnomalyRecord> SEVERITY_COMPARATOR = Comparator
            .comparingInt((AnomalyRecord a) -> SEVERITY_ORDER.getOrDefault(a.getSeverity(), 99))
            .thenComparing(a -> a.getAnomalyType() == null ? "" : a.getAnomalyType())
            .thenComparing(a -> a.getItemName() == null ? "" : a.getItemName());

    private final ReportRepository reportRepo;
    private final AnomalyRepository anomalyRepo;

    public AnomalyQueryService(ReportRepository reportRepo, AnomalyRepository anomalyRepo) {
        this.reportRepo = reportRepo;
        this.anomalyRepo = anomalyRepo;
    }

    /**
     * 查询某份报告的所有异常，按严重度降序排列。
     *
     * @param reportId 报告 ID
     * @param userId   当前用户 ID
     * @return 异常列表 Flux；report 不存在或无权限抛 {@code REPORT_NOT_FOUND}
     */
    public Flux<AnomalyResponse> listAnomalies(Long reportId, Long userId) {
        log.debug("[AnomalyQueryService] listAnomalies reportId={} userId={}", reportId, userId);
        return assertReportOwnership(reportId, userId)
                .thenMany(anomalyRepo.findByReportIdOrderByAnomalyTypeAscItemNameAsc(reportId)
                        .collectSortedList(SEVERITY_COMPARATOR)
                        .flatMapMany(Flux::fromIterable)
                        .map(AnomalyQueryService::toResponse));
    }

    private Mono<Report> assertReportOwnership(Long reportId, Long userId) {
        return reportRepo.findById(reportId)
                .filter(report -> userId.equals(report.getUserId()))
                .switchIfEmpty(Mono.error(new BusinessException(
                        HttpStatus.NOT_FOUND, "REPORT_NOT_FOUND",
                        "报告不存在: " + reportId)));
    }

    private static AnomalyResponse toResponse(AnomalyRecord anomaly) {
        return new AnomalyResponse(
                anomaly.getId(),
                anomaly.getItemName(),
                anomaly.getAnomalyType(),
                anomaly.getMetricValue(),
                anomaly.getThreshold(),
                anomaly.getDescription(),
                anomaly.getSeverity(),
                anomaly.getCreatedAt() != null
                        ? anomaly.getCreatedAt().atZone(ZoneOffset.UTC).toInstant()
                        : null);
    }
}
