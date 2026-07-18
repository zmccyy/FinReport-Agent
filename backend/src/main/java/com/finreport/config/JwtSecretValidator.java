package com.finreport.config;

import jakarta.annotation.PostConstruct;

import org.springframework.core.env.Environment;
import org.springframework.core.env.Profiles;
import org.springframework.stereotype.Component;

/**
 * 在应用启动时验证 JWT 签名密钥，避免生产环境使用公开或弱密钥。
 */
@Component
public class JwtSecretValidator {

    private static final int MIN_SECRET_LENGTH = 32;

    private final JwtConfig jwtConfig;
    private final Environment environment;

    public JwtSecretValidator(JwtConfig jwtConfig, Environment environment) {
        this.jwtConfig = jwtConfig;
        this.environment = environment;
    }

    /**
     * 验证已绑定的 JWT 配置；不符合要求时阻止应用启动。
     */
    @PostConstruct
    void validate() {
        String secret = jwtConfig.getSecret();
        if (secret == null || secret.isBlank() || secret.length() < MIN_SECRET_LENGTH) {
            throw new IllegalStateException("JWT_SECRET 必须至少包含 32 个字符");
        }
        boolean localProfile = environment.acceptsProfiles(Profiles.of("local"));
        if (!localProfile && JwtConfig.LOCAL_DEVELOPMENT_SECRET.equals(secret)) {
            throw new IllegalStateException("非 local 环境必须通过 JWT_SECRET 提供独立 JWT 密钥");
        }
    }
}
