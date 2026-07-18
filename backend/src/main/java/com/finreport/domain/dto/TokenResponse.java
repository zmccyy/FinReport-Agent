package com.finreport.domain.dto;

/**
 * 认证令牌响应 DTO。
 *
 * <p>登录/刷新成功后返回客户端。Access Token 用于 API 鉴权，
 * Refresh Token 用于无感刷新。</p>
 *
 * @param accessToken  JWT Access Token（有效期 1h）
 * @param refreshToken JWT Refresh Token（有效期 7d）
 * @param expiresIn    Access Token 剩余有效秒数
 * @param tokenType    Token 类型，固定为 "Bearer"
 */
public record TokenResponse(
        String accessToken,
        String refreshToken,
        long expiresIn,
        String tokenType
) {
    public TokenResponse(String accessToken, String refreshToken, long expiresIn) {
        this(accessToken, refreshToken, expiresIn, "Bearer");
    }
}
