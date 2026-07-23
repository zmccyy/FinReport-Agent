package com.finreport.domain.dto;

import java.time.Instant;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/**
 * 报告产物响应 — spec §6.2.2 / M3.09。
 *
 * <p>{@code GET /api/v1/reports/{reportId}/artifacts} 的返回体单条。
 * 包含产物类型、MinIO object key、状态以及预签名下载 URL。</p>
 *
 * @param id           产物记录 ID
 * @param artifactType 产物类型（PDF / MARKDOWN / CHART_PIE / CHART_LINE / CHART_BAR）
 * @param objectKey    MinIO 对象 key
 * @param status       状态（GENERATED / FAILED）
 * @param downloadUrl  预签名下载 URL（status=FAILED 时返回空字符串）
 * @param createdAt    创建时间（ISO-8601）
 */
@JsonPropertyOrder({
        "id", "artifactType", "objectKey", "status", "downloadUrl", "createdAt"
})
public record ReportArtifactResponse(
        Long id,
        String artifactType,
        String objectKey,
        String status,
        String downloadUrl,
        Instant createdAt
) {}
