package com.finreport.repository;

import org.springframework.data.repository.reactive.ReactiveCrudRepository;
import org.springframework.stereotype.Repository;

import com.finreport.domain.entity.AccountingCheck;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 勾稽核对结果 Repository — 对应 accounting_check 表（spec §5.2.2 §2.3）。
 *
 * <p>M3.04 提供 {@code CheckResultWriter} 写入路径 + 简单查询接口；
 * M3.08 前端勾稽页会复用此 repo 做条件查询。</p>
 */
@Repository
public interface AccountingCheckRepository
        extends ReactiveCrudRepository<AccountingCheck, Long> {

    /**
     * 统计某份报告的勾稽规则记录数（用于验收「单份年报产生 3 条 check」）。
     *
     * @param reportId 报告 ID
     * @return 记录数
     */
    Mono<Long> countByReportId(Long reportId);

    /**
     * 按报告 ID 查询所有勾稽结果，按规则类型排序（保证展示稳定）。
     *
     * @param reportId 报告 ID
     * @return 勾稽结果列表
     */
    Flux<AccountingCheck> findByReportIdOrderByRuleTypeAsc(Long reportId);

    /**
     * 删除某份报告的所有勾稽结果（重抽场景使用，避免残留脏数据）。
     *
     * @param reportId 报告 ID
     * @return 已删除行数
     */
    Mono<Long> deleteByReportId(Long reportId);
}
