package com.finreport.domain.dto;

import java.math.BigDecimal;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/**
 * 三表科目条目响应 — spec §6.2.2 / M2.11。
 *
 * <p>对应 {@code financial_statement} 表一行的前端展示视图。字段命名与
 * L3 {@code StatementType.value}（{@code balance_sheet} /
 * {@code income_statement} / {@code cash_flow}）以及表结构
 * （spec §5.2.2）保持一致，避免在 L2/L1 边界再做映射。</p>
 *
 * @param id            科目行 ID
 * @param statementType 表类型：balance_sheet / income_statement / cash_flow
 * @param itemName      科目名（如「货币资金」）
 * @param itemValue     科目数值（DECIMAL(20,4)，可为 null 表示缺失）
 * @param currency      币种（默认 CNY）
 * @param unit          单位（默认「元」）
 * @param scope         范围（合并 / 母公司）
 * @param periodType    期间类型（本期 / 上期）
 * @param confidence    抽取置信度 0-1
 * @param sourcePage    源 PDF 页码（用于回溯定位）
 */
@JsonPropertyOrder({
        "id", "statementType", "itemName", "itemValue",
        "currency", "unit", "scope", "periodType",
        "confidence", "sourcePage"
})
public record StatementItemResponse(
        Long id,
        String statementType,
        String itemName,
        BigDecimal itemValue,
        String currency,
        String unit,
        String scope,
        String periodType,
        BigDecimal confidence,
        Integer sourcePage
) {}
