package com.finreport.domain.entity;

import java.math.BigDecimal;
import java.time.LocalDateTime;

import org.springframework.data.annotation.Id;
import org.springframework.data.relational.core.mapping.Column;
import org.springframework.data.relational.core.mapping.Table;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 财报科目明细实体，映射 financial_statement 表 — V2__init_report.sql §2.2。
 *
 * <p>每一行对应一张报表（BS/IS/CF）中的一个科目条目，由 L3 Extractor 抽取后
 * 通过 progress 消息回传 L2，再由 {@code StatementWriter} 写入。</p>
 *
 * <p>{@code statementType} 字段值与 L3 {@code StatementType.value} 一致
 * （{@code balance_sheet} / {@code income_statement} / {@code cash_flow}），
 * 避免在 L2/L3 边界再做枚举映射。</p>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Table("financial_statement")
public class FinancialStatementItem {

    @Id
    private Long id;

    @Column("report_id")
    private Long reportId;

    @Column("statement_type")
    private String statementType;

    @Column("item_name")
    private String itemName;

    @Column("item_value")
    private BigDecimal itemValue;

    private String currency;

    private String unit;

    private String scope;

    @Column("period_type")
    private String periodType;

    private BigDecimal confidence;

    @Column("source_page")
    private Integer sourcePage;

    @Column("source_bbox")
    private String sourceBbox;

    @Column("created_at")
    private LocalDateTime createdAt;
}
