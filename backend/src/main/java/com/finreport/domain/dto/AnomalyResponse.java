package com.finreport.domain.dto;

import java.math.BigDecimal;
import java.time.Instant;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/**
 * 异常检测结果响应 — spec §6.2.2 / M3.09。
 *
 * <p>{@code GET /api/v1/reports/{reportId}/anomalies} 的返回体单条。
 * 字段与 {@code anomaly} 表对齐。</p>
 *
 * @param id          异常记录 ID
 * @param itemName    科目名
 * @param anomalyType 异常类型（yoy_change / qoq_change / logic_conflict）
 * @param metricValue 指标值
 * @param threshold   阈值
 * @param description 异常描述
 * @param severity    严重度（INFO / WARN / ERROR / CRITICAL）
 * @param createdAt   创建时间（ISO-8601）
 */
@JsonPropertyOrder({
        "id", "itemName", "anomalyType", "metricValue",
        "threshold", "description", "severity", "createdAt"
})
public record AnomalyResponse(
        Long id,
        String itemName,
        String anomalyType,
        BigDecimal metricValue,
        BigDecimal threshold,
        String description,
        String severity,
        Instant createdAt
) {}
