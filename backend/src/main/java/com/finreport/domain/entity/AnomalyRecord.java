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
 * 异常检测结果实体，映射 anomaly 表 — V2__init_report.sql §2.4。
 *
 * <p>每一行对应 AnomalyDetector 产出的一条异常（同比/环比/逻辑异常），
 * 由 L3 Reasoner 计算后通过 progress 消息回传 L2，再由
 * {@code CheckResultWriter} 写入。</p>
 *
 * <p>类名用 {@code AnomalyRecord} 而非 {@code Anomaly}，避免与 L3 schema
 * 概念混淆；表名仍是 {@code anomaly}（spec §5.2.2）。</p>
 *
 * <p>{@code anomalyType} 字段值与 L3 {@code AnomalyType.value} 一致
 * （{@code yoy_change} / {@code qoq_change} / {@code logic_conflict}）。
 * {@code severity} 字段值与 L3 {@code Severity.value} 一致。</p>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Table("anomaly")
public class AnomalyRecord {

    @Id
    private Long id;

    @Column("report_id")
    private Long reportId;

    @Column("item_name")
    private String itemName;

    @Column("anomaly_type")
    private String anomalyType;

    @Column("metric_value")
    private BigDecimal metricValue;

    private BigDecimal threshold;

    private String description;

    private String severity;

    @Column("created_at")
    private LocalDateTime createdAt;
}
