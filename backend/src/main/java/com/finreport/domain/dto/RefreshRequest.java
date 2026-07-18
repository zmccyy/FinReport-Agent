package com.finreport.domain.dto;

import jakarta.validation.constraints.NotBlank;

/**
 * Token 刷新请求 DTO。
 */
public record RefreshRequest(
        @NotBlank(message = "Refresh Token 不能为空")
        String refreshToken
) {}
