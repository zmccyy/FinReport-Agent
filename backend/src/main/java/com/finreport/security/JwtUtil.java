package com.finreport.security;

import java.util.Date;

import javax.crypto.SecretKey;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import com.finreport.config.JwtConfig;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;

/**
 * JWT 工具类 — Token 生成、解析、校验。
 *
 * <p>使用 HMAC-SHA256 签名。Access Token 1h，Refresh Token 7d。</p>
 */
@Component
public class JwtUtil {

    private static final Logger log = LoggerFactory.getLogger(JwtUtil.class);

    private final SecretKey secretKey;
    private final JwtConfig jwtConfig;

    public JwtUtil(JwtConfig jwtConfig) {
        this.jwtConfig = jwtConfig;
        this.secretKey = Keys.hmacShaKeyFor(jwtConfig.getSecret().getBytes());
        log.debug("[JwtUtil] JWT 签名密钥已加载");
    }

    /**
     * 生成 Access Token。
     *
     * @param userId   用户 ID
     * @param username 用户名
     * @return 签名的 JWT 字符串
     */
    public String generateAccessToken(Long userId, String username) {
        return buildToken(userId, username, jwtConfig.getAccessExpiration() * 1000);
    }

    /**
     * 生成 Refresh Token（有效期更长）。
     */
    public String generateRefreshToken(Long userId, String username) {
        return buildToken(userId, username, jwtConfig.getRefreshExpiration() * 1000);
    }

    /**
     * 解析并校验 Token，返回 Claims。
     *
     * @param token JWT 字符串
     * @return Claims 对象
     * @throws JwtException 解析或签名校验失败
     */
    public Claims parseToken(String token) {
        return Jwts.parser()
                .verifyWith(secretKey)
                .build()
                .parseSignedClaims(token)
                .getPayload();
    }

    /**
     * 从 Token 中提取 userId。
     */
    public Long getUserId(String token) {
        return parseToken(token).get("userId", Long.class);
    }

    /**
     * 从 Token 中提取 username。
     */
    public String getUsername(String token) {
        return parseToken(token).getSubject();
    }

    /**
     * 检查 Token 是否已过期。
     */
    public boolean isExpired(String token) {
        try {
            return parseToken(token).getExpiration().before(new Date());
        } catch (JwtException e) {
            return true;
        }
    }

    /**
     * 验证 Token 格式和签名。
     *
     * @param token JWT 字符串（可为 null）
     * @return true 如果 Token 有效
     */
    public boolean validate(String token) {
        if (token == null || token.isEmpty()) {
            return false;
        }
        try {
            parseToken(token);
            return true;
        } catch (JwtException | IllegalArgumentException e) {
            log.debug("[JwtUtil] Token 校验失败: {}", e.getMessage());
            return false;
        }
    }

    // --- private ---

    private String buildToken(Long userId, String username, long expirationMs) {
        Date now = new Date();
        Date expiry = new Date(now.getTime() + expirationMs);
        return Jwts.builder()
                .issuer(jwtConfig.getIssuer())
                .subject(username)
                .claim("userId", userId)
                .issuedAt(now)
                .expiration(expiry)
                .signWith(secretKey)
                .compact();
    }
}
