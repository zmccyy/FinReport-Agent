package com.finreport.domain.dto;

import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/**
 * 文件上传响应 — spec §6.3.1。
 */
@JsonPropertyOrder({"taskId", "reportId", "status"})
public record UploadResponse(
        String taskId,
        Long reportId,
        String status
) {}
