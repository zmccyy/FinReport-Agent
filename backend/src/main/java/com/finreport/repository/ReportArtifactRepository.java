package com.finreport.repository;

import org.springframework.data.repository.reactive.ReactiveCrudRepository;
import org.springframework.stereotype.Repository;

import com.finreport.domain.entity.ReportArtifact;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 报告产物 Repository — 对应 report_artifact 表（spec §5.2.2 §2.5）。
 *
 * <p>M3.08 提供 {@code ReportArtifactWriter} 写入路径 + 幂等查询接口；
 * M3.09 前端报告页通过 (reportId, artifactType) 查找下载链接。</p>
 *
 * <p>V5__init_indexes.sql §25 已建 {@code idx_artifact_type_status}，
 * {@link #findByReportIdAndArtifactType} 走此索引。</p>
 */
@Repository
public interface ReportArtifactRepository
        extends ReactiveCrudRepository<ReportArtifact, Long> {

    /**
     * 幂等检查 — 同一报告同类型产物是否已存在（重放 REPORT SUCCESS 时使用）。
     *
     * @param reportId    报告 ID
     * @param artifactType 产物类型（PDF / MARKDOWN / CHART_PIE 等）
     * @return 已存在的产物（可能为 empty）
     */
    Mono<ReportArtifact> findByReportIdAndArtifactType(Long reportId, String artifactType);

    /**
     * 按报告 ID 查询所有产物，按类型排序保证展示稳定。
     *
     * @param reportId 报告 ID
     * @return 产物列表
     */
    Flux<ReportArtifact> findByReportIdOrderByArtifactTypeAsc(Long reportId);

    /**
     * 统计某份报告的产物数量（用于验收「PDF + MD + 3 PNG = 5 行」）。
     *
     * @param reportId 报告 ID
     * @return 记录数
     */
    Mono<Long> countByReportId(Long reportId);

    /**
     * 删除某份报告的所有产物（重抽场景使用，避免残留脏数据）。
     *
     * @param reportId 报告 ID
     * @return 已删除行数
     */
    Mono<Long> deleteByReportId(Long reportId);
}
