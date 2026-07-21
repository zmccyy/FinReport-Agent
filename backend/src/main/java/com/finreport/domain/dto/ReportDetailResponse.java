package com.finreport.domain.dto;

import java.time.Instant;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/**
 * 财报详情响应 — spec §6.2.2 / M2.11。
 *
 * <p>{@code GET /api/v1/reports/{reportId}} 的返回体。
 * 用于详情页头部展示公司信息、报告期间、PDF 元数据等。
 * 字段命名与 {@code report} 表对齐。</p>
 *
 * @param id            财报 ID
 * @param taskId        关联任务 ID（用于跳转进度页）
 * @param companyCode   公司股票代码
 * @param companyName    公司简称
 * @param reportType     报告类型（ANNUAL / SEMI / Q1 / Q3）
 * @param reportPeriod   报告期间（如 2024-12-31）
 * @param pageCount      PDF 页数
 * @param parseStatus    解析状态（PENDING / RUNNING / COMPLETED / FAILED）
 * @param pdfObjectKey   MinIO 对象 key（用于生成预签名下载 URL）
 * @param createdAt      创建时间（ISO-8601）
 */
@JsonPropertyOrder({
        "id", "taskId", "companyCode", "companyName",
        "reportType", "reportPeriod", "pageCount",
        "parseStatus", "pdfObjectKey", "createdAt"
})
public record ReportDetailResponse(
        Long id,
        String taskId,
        String companyCode,
        String companyName,
        String reportType,
        String reportPeriod,
        Integer pageCount,
        String parseStatus,
        String pdfObjectKey,
        Instant createdAt
) {}
