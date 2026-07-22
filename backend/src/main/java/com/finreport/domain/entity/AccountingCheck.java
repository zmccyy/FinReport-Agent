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
 * 勾稽核对结果实体，映射 accounting_check 表 — V2__init_report.sql §2.3。
 *
 * <p>每一行对应一条勾稽规则（资产=负债+所有者权益 / 净利润→未分配利润变动 /
 * 经营现金流 vs 净利润）的执行结果，由 L3 Reasoner 计算后通过 progress
 * 消息回传 L2，再由 {@code CheckResultWriter} 写入。</p>
 *
 * <p>{@code ruleType} 字段值与 L3 {@code RuleType.value} 一致
 * （{@code balance_sheet_identity} / {@code net_income_to_retained} /
 * {@code cash_flow_vs_net_income}），避免在 L2/L3 边界再做枚举映射。
 * {@code severity} 字段值与 L3 {@code Severity.value} 一致
 * （{@code INFO} / {@code WARN} / {@code ERROR} / {@code CRITICAL}）。</p>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Table("accounting_check")
public class AccountingCheck {

    @Id
    private Long id;

    @Column("report_id")
    private Long reportId;

    @Column("rule_name")
    private String ruleName;

    @Column("rule_type")
    private String ruleType;

    private BigDecimal expected;

    private BigDecimal actual;

    private BigDecimal diff;

    @Column("is_pass")
    private Boolean isPass;

    private String severity;

    private String note;

    @Column("created_at")
    private LocalDateTime createdAt;
}
