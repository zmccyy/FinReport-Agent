package com.finreport.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * JWT 配置属性。
 *
 * <p>从 application.yml 的 {@code jwt} 前缀读取。
 * M1.08 启用认证后使用。
 * 通过 @EnableConfigurationProperties 在 FinReportApplication 中注册。</p>
 */
@ConfigurationProperties(prefix = "jwt")
public class JwtConfig {

    /** 仅允许在 local profile 中使用的开发密钥。 */
    static final String LOCAL_DEVELOPMENT_SECRET =
            "finreport-local-development-secret-only-change-before-deployment";

    /** JWT 签名密钥（至少 256 bits） */
    private String secret;

    /** Access Token 有效期（秒），默认 1 小时 */
    private long accessExpiration = 3600;

    /** Refresh Token 有效期（秒），默认 7 天 */
    private long refreshExpiration = 604800;

    /** Token 签发者 */
    private String issuer = "finreport";

    // getters / setters

    public String getSecret() { return secret; }
    public void setSecret(String secret) { this.secret = secret; }
    public long getAccessExpiration() { return accessExpiration; }
    public void setAccessExpiration(long accessExpiration) { this.accessExpiration = accessExpiration; }
    public long getRefreshExpiration() { return refreshExpiration; }
    public void setRefreshExpiration(long refreshExpiration) { this.refreshExpiration = refreshExpiration; }
    public String getIssuer() { return issuer; }
    public void setIssuer(String issuer) { this.issuer = issuer; }
}
