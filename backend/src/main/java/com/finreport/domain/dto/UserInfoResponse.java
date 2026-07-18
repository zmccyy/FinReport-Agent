package com.finreport.domain.dto;

import java.time.Instant;

/**
 * 当前用户信息响应 DTO。
 *
 * <p>用于 GET /api/v1/users/me 接口。</p>
 */
public record UserInfoResponse(
        Long id,
        String username,
        String email,
        String role,
        Instant createdAt
) {}
