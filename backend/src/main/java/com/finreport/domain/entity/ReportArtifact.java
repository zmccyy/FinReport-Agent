package com.finreport.domain.entity;

import java.time.LocalDateTime;

import org.springframework.data.annotation.Id;
import org.springframework.data.relational.core.mapping.Column;
import org.springframework.data.relational.core.mapping.Table;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 报告产物实体，映射 report_artifact 表 — V2__init_report.sql §2.5。
 *
 * <p>每一行记录一份 L3 M10 生成的产物（PDF / Markdown / 图表 PNG）在 MinIO
 * 中的位置，由 {@code ReportArtifactWriter} 在 REPORT step SUCCESS 时写入。
 * 前端 {@code ReportViewer} 通过 (reportId, artifactType) 查找预签名 URL 下载。</p>
 *
 * <p>{@code artifactType} 字段值与 spec §5.3 object key 命名对齐
 * （PDF / MARKDOWN / CHART_PIE / CHART_LINE / CHART_BAR）；
 * {@code status} 取值 GENERATED / FAILED（spec §2.5 默认 GENERATED）。
 * V5__init_indexes.sql §25 已建 {@code idx_artifact_type_status} 支撑幂等查询。</p>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Table("report_artifact")
public class ReportArtifact {

    /**
     * 产物状态 — 生成成功（spec §2.5 默认值）。
     */
    public static final String STATUS_GENERATED = "GENERATED";

    /**
     * 产物状态 — 上传 MinIO 失败（保留行供排查）。
     */
    public static final String STATUS_FAILED = "FAILED";

    /**
     * artifact_type = PDF（spec §5.3 reports/{reportId}/report.pdf）。
     */
    public static final String TYPE_PDF = "PDF";

    /**
     * artifact_type = MARKDOWN（spec §5.3 reports/{reportId}/report.md）。
     */
    public static final String TYPE_MARKDOWN = "MARKDOWN";

    /**
     * artifact_type = CHART_PIE（spec §5.3 charts/chart_pie.png）。
     */
    public static final String TYPE_CHART_PIE = "CHART_PIE";

    /**
     * artifact_type = CHART_LINE（spec §5.3 charts/chart_line.png）。
     */
    public static final String TYPE_CHART_LINE = "CHART_LINE";

    /**
     * artifact_type = CHART_BAR（spec §5.3 charts/chart_bar.png）。
     */
    public static final String TYPE_CHART_BAR = "CHART_BAR";

    @Id
    private Long id;

    @Column("report_id")
    private Long reportId;

    @Column("artifact_type")
    private String artifactType;

    @Column("object_key")
    private String objectKey;

    private String status;

    @Column("created_at")
    private LocalDateTime createdAt;
}
