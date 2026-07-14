package com.finreport;

import static org.junit.jupiter.api.Assertions.*;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import com.finreport.config.JwtConfig;
import com.finreport.exception.AuthException;
import com.finreport.exception.BusinessException;
import com.finreport.exception.IntegrationException;
import com.finreport.exception.ValidationException;
import com.finreport.security.JwtUtil;

/**
 * M1.07 SpringBoot 骨架单元测试。
 *
 * <p>覆盖：异常体系、JWT 生成/校验、Token 过期判断。
 * 不依赖外部服务（MySQL/Redis/RabbitMQ）。</p>
 */
class M107SkeletonTest {

    // ========================================================================
    // 异常体系
    // ========================================================================

    @Nested
    @DisplayName("异常体系 — RFC 9457")
    class ExceptionHierarchy {

        @Test
        @DisplayName("BusinessException 应包含 HTTP 状态码和错误代码")
        void shouldContainHttpStatusAndErrorCode() {
            BusinessException ex = new BusinessException(
                    org.springframework.http.HttpStatus.UNPROCESSABLE_ENTITY,
                    "TEST_ERROR",
                    "测试错误"
            );
            assertEquals(422, ex.getStatus().value());
            assertEquals("TEST_ERROR", ex.getErrorCode());
            assertEquals("测试错误", ex.getMessage());
        }

        @Test
        @DisplayName("AuthException 默认状态码为 401")
        void shouldDefaultTo401() {
            AuthException ex = new AuthException("TOKEN_EXPIRED", "Token 已过期");
            assertEquals(401, ex.getStatus().value());
            assertEquals("TOKEN_EXPIRED", ex.getErrorCode());
        }

        @Test
        @DisplayName("AuthException 支持自定义状态码（如 403）")
        void shouldSupportCustomStatus() {
            AuthException ex = new AuthException(
                    org.springframework.http.HttpStatus.FORBIDDEN,
                    "INSUFFICIENT_ROLE",
                    "权限不足"
            );
            assertEquals(403, ex.getStatus().value());
        }

        @Test
        @DisplayName("ValidationException 默认状态码为 422")
        void shouldUse422ForValidation() {
            ValidationException ex = new ValidationException("文件大小超过限制");
            assertEquals(422, ex.getStatus().value());
            assertEquals("VALIDATION_ERROR", ex.getErrorCode());
        }

        @Test
        @DisplayName("ValidationException 支持自定义错误代码")
        void shouldSupportCustomErrorCode() {
            ValidationException ex = new ValidationException("FILE_TOO_LARGE", "文件超过 50MB");
            assertEquals("FILE_TOO_LARGE", ex.getErrorCode());
        }

        @Test
        @DisplayName("IntegrationException 应包含 HTTP 状态码和错误代码")
        void shouldContainStatusAndCode() {
            IntegrationException ex = new IntegrationException(
                    org.springframework.http.HttpStatus.BAD_GATEWAY,
                    "AI_SERVICE_DOWN",
                    "AI 服务不可用"
            );
            assertEquals(502, ex.getStatus().value());
            assertEquals("AI_SERVICE_DOWN", ex.getErrorCode());
        }

        @Test
        @DisplayName("IntegrationException 应支持 cause 链")
        void shouldSupportCauseChain() {
            RuntimeException cause = new RuntimeException("连接超时");
            IntegrationException ex = new IntegrationException(
                    org.springframework.http.HttpStatus.BAD_GATEWAY,
                    "MQ_ERROR",
                    "MQ 投递失败",
                    cause
            );
            assertSame(cause, ex.getCause());
        }
    }

    // ========================================================================
    // JWT 工具类
    // ========================================================================

    @Nested
    @DisplayName("JWT 工具 — 生成/校验/过期")
    class JwtUtilTests {

        private final JwtConfig jwtConfig = createTestConfig();
        private final JwtUtil jwtUtil = new JwtUtil(jwtConfig);

        private static JwtConfig createTestConfig() {
            JwtConfig config = new JwtConfig();
            config.setSecret("test-jwt-secret-at-least-256-bits-long-for-hmac-sha256");
            config.setAccessExpiration(3600);
            config.setRefreshExpiration(604800);
            config.setIssuer("finreport-test");
            return config;
        }

        @Test
        @DisplayName("应生成有效的 Access Token")
        void shouldGenerateAccessToken() {
            String token = jwtUtil.generateAccessToken(1L, "admin");
            assertNotNull(token);
            assertFalse(token.isEmpty());
            // JWT 格式：header.payload.signature
            String[] parts = token.split("\\.");
            assertEquals(3, parts.length);
        }

        @Test
        @DisplayName("应生成有效的 Refresh Token")
        void shouldGenerateRefreshToken() {
            String token = jwtUtil.generateRefreshToken(1L, "admin");
            assertNotNull(token);
            assertTrue(jwtUtil.validate(token));
        }

        @Test
        @DisplayName("应能从 Token 中提取 username")
        void shouldExtractUsername() {
            String token = jwtUtil.generateAccessToken(1L, "testuser");
            assertEquals("testuser", jwtUtil.getUsername(token));
        }

        @Test
        @DisplayName("应能从 Token 中提取 userId")
        void shouldExtractUserId() {
            String token = jwtUtil.generateAccessToken(42L, "admin");
            assertEquals(42L, jwtUtil.getUserId(token));
        }

        @Test
        @DisplayName("有效 Token 应通过 validate 检查")
        void shouldValidateValidToken() {
            String token = jwtUtil.generateAccessToken(1L, "admin");
            assertTrue(jwtUtil.validate(token));
        }

        @Test
        @DisplayName("无效 Token 不应通过 validate 检查")
        void shouldRejectInvalidToken() {
            assertFalse(jwtUtil.validate("invalid.token.here"));
            assertFalse(jwtUtil.validate(""));
            assertFalse(jwtUtil.validate("not.a.jwt"));
        }

        @Test
        @DisplayName("空 Token 不应通过 validate")
        void shouldRejectNullToken() {
            assertFalse(jwtUtil.validate(null));
        }

        @Test
        @DisplayName("已过期 Token 应被 isExpired 检测")
        void shouldDetectExpiredToken() {
            JwtConfig shortConfig = new JwtConfig();
            shortConfig.setSecret("test-jwt-secret-at-least-256-bits-long-for-hmac-sha256");
            shortConfig.setAccessExpiration(-1); // 立即过期
            shortConfig.setRefreshExpiration(604800);
            shortConfig.setIssuer("finreport-test");
            JwtUtil shortJwtUtil = new JwtUtil(shortConfig);

            String token = shortJwtUtil.generateAccessToken(1L, "admin");
            assertTrue(shortJwtUtil.isExpired(token));
        }

        @Test
        @DisplayName("不同密钥签名的 Token 应被拒绝")
        void shouldRejectDifferentKey() {
            String token = jwtUtil.generateAccessToken(1L, "admin");

            JwtConfig otherConfig = new JwtConfig();
            otherConfig.setSecret("other-secret-key-at-least-256-bits-long-for-hmac-sha256!!!");
            otherConfig.setAccessExpiration(3600);
            otherConfig.setRefreshExpiration(604800);
            otherConfig.setIssuer("finreport-test");
            JwtUtil otherJwtUtil = new JwtUtil(otherConfig);

            assertFalse(otherJwtUtil.validate(token));
        }

        @Test
        @DisplayName("应正确设置 Token 签发者")
        void shouldSetIssuer() {
            String token = jwtUtil.generateAccessToken(1L, "admin");
            String issuer = jwtUtil.parseToken(token).getIssuer();
            assertEquals("finreport-test", issuer);
        }
    }

    // ========================================================================
    // JwtConfig
    // ========================================================================

    @Nested
    @DisplayName("JwtConfig — 默认值")
    class JwtConfigDefaults {

        @Test
        @DisplayName("默认 Access Token 有效期应为 3600 秒")
        void shouldDefaultAccessExpirationTo3600() {
            JwtConfig config = new JwtConfig();
            assertEquals(3600, config.getAccessExpiration());
        }

        @Test
        @DisplayName("默认 Refresh Token 有效期应为 604800 秒（7 天）")
        void shouldDefaultRefreshExpirationTo604800() {
            JwtConfig config = new JwtConfig();
            assertEquals(604800, config.getRefreshExpiration());
        }

        @Test
        @DisplayName("默认签发者应为 finreport")
        void shouldDefaultIssuerToFinreport() {
            JwtConfig config = new JwtConfig();
            assertEquals("finreport", config.getIssuer());
        }

        @Test
        @DisplayName("应支持 setter 修改配置")
        void shouldSupportSetter() {
            JwtConfig config = new JwtConfig();
            config.setSecret("new-secret");
            config.setAccessExpiration(7200);
            config.setRefreshExpiration(1209600);
            config.setIssuer("custom");

            assertEquals("new-secret", config.getSecret());
            assertEquals(7200, config.getAccessExpiration());
            assertEquals(1209600, config.getRefreshExpiration());
            assertEquals("custom", config.getIssuer());
        }
    }
}
