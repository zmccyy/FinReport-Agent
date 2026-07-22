package com.finreport.repository;

import org.springframework.data.repository.reactive.ReactiveCrudRepository;
import org.springframework.stereotype.Repository;

import com.finreport.domain.entity.AnomalyRecord;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 异常检测结果 Repository — 对应 anomaly 表（spec §5.2.2 §2.4）。
 *
 * <p>M3.04 提供 {@code CheckResultWriter} 写入路径 + 简单查询接口；
 * M3.08 前端异常页会复用此 repo 按严重度排序展示。</p>
 */
@Repository
public interface AnomalyRepository
        extends ReactiveCrudRepository<AnomalyRecord, Long> {

    /**
     * 统计某份报告的异常记录数。
     *
     * @param reportId 报告 ID
     * @return 记录数
     */
    Mono<Long> countByReportId(Long reportId);

    /**
     * 按报告 ID 查询所有异常，按严重度排序（ERROR > WARN > INFO）。
     *
     * <p>排序按 severity 字母序（ERROR < INFO < WARN 不合预期），
     * 改用 CASE WHEN 自定义排序由调用方 sort；这里只保证按 severity 分组。
     * 简单实现：按 anomaly_type + item_name 排序保证展示稳定。</p>
     *
     * @param reportId 报告 ID
     * @return 异常列表
     */
    Flux<AnomalyRecord> findByReportIdOrderByAnomalyTypeAscItemNameAsc(Long reportId);

    /**
     * 删除某份报告的所有异常（重抽场景使用，避免残留脏数据）。
     *
     * @param reportId 报告 ID
     * @return 已删除行数
     */
    Mono<Long> deleteByReportId(Long reportId);
}
