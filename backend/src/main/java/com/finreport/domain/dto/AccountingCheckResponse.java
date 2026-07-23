package com.finreport.domain.dto;

import java.math.BigDecimal;
import java.time.Instant;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/**
 * 勾稽核对结果响应 — spec §6.2.2 / M3.09。
 *
 * <p>{@code GET /api/v1/reports/{reportId}/checks} 的返回体单条。
 * 字段与 {@code accounting_check} 表对齐。</p>
 *
 * @param id       勾稽记录 ID
 * @param ruleName 规则名称（如「资产负债表恒等式」）
 * @param ruleType 规则类型（balance_sheet_identity / net_income_to_retained / cash_flow_vs_net_income）
 * @param expected 预期值
 * @param actual   实际值
 * @param diff     差额
 * @param isPass   是否通过
 * @param severity 严重度（INFO / WARN / ERROR / CRITICAL）
 * @param note     说明
 * @param createdAt 创建时间（ISO-8601）
 */
@JsonPropertyOrder({
        "id", "ruleName", "ruleType", "expected", "actual",
        "diff", "isPass", "severity", "note", "createdAt"
})
public record AccountingCheckResponse(
        Long id,
        String ruleName,
        String ruleType,
        BigDecimal expected,
        BigDecimal actual,
        BigDecimal diff,
        Boolean isPass,
        String severity,
        String note,
        Instant createdAt
) {}
