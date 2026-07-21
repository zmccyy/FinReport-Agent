package com.finreport.repository;

import org.springframework.data.repository.reactive.ReactiveCrudRepository;
import org.springframework.stereotype.Repository;

import com.finreport.domain.entity.FinancialStatementItem;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 财报科目明细 Repository — 对应 financial_statement 表（spec §5.2.2）。
 *
 * <p>M2.09 提供 {@code StatementWriter} 写入路径 + 简单查询接口；
 * M3 勾稽规则引擎 / M5 Agent 工具会复用此 repo 做条件查询。</p>
 */
@Repository
public interface FinancialStatementRepository
        extends ReactiveCrudRepository<FinancialStatementItem, Long> {

    /**
     * 统计某份报告的科目行数（用于验收「三表共 ~150 条」）。
     *
     * @param reportId 报告 ID
     * @return 科目行数
     */
    Mono<Long> countByReportId(Long reportId);

    /**
     * 统计某份报告某张表的科目行数。
     *
     * @param reportId      报告 ID
     * @param statementType 表类型（balance_sheet / income_statement / cash_flow）
     * @return 科目行数
     */
    Mono<Long> countByReportIdAndStatementType(Long reportId, String statementType);

    /**
     * 按报告 ID 查询所有科目，按表类型 + 科目名排序。
     *
     * @param reportId 报告 ID
     * @return 科目列表
     */
    Flux<FinancialStatementItem> findByReportIdOrderByStatementTypeAscItemNameAsc(Long reportId);

    /**
     * 按报告 ID + 表类型查询科目。
     *
     * @param reportId      报告 ID
     * @param statementType 表类型
     * @return 科目列表
     */
    Flux<FinancialStatementItem> findByReportIdAndStatementType(Long reportId, String statementType);

    /**
     * 删除某份报告的所有科目（重抽场景使用，避免残留脏数据）。
     *
     * @param reportId 报告 ID
     * @return 已删除行数
     */
    Mono<Long> deleteByReportId(Long reportId);
}
