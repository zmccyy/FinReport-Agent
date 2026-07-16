package com.finreport.config;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;

/**
 * {@link JwtSecretValidator} 单元测试。
 */
@DisplayName("JwtSecretValidator")
class JwtSecretValidatorTest {

    @Test
    @DisplayName("should allow the dedicated development secret only in local profile")
    void shouldAllowDedicatedDevelopmentSecretOnlyInLocalProfile() {
        JwtConfig config = new JwtConfig();
        config.setSecret(JwtConfig.LOCAL_DEVELOPMENT_SECRET);
        MockEnvironment environment = new MockEnvironment();
        environment.setActiveProfiles("local");

        assertDoesNotThrow(() -> new JwtSecretValidator(config, environment).validate());
    }

    @Test
    @DisplayName("should reject development secret outside local profile")
    void shouldRejectDevelopmentSecretOutsideLocalProfile() {
        JwtConfig config = new JwtConfig();
        config.setSecret(JwtConfig.LOCAL_DEVELOPMENT_SECRET);
        MockEnvironment environment = new MockEnvironment();
        environment.setActiveProfiles("docker");

        assertThrows(IllegalStateException.class,
                () -> new JwtSecretValidator(config, environment).validate());
    }

    @Test
    @DisplayName("should reject missing or weak secret")
    void shouldRejectMissingOrWeakSecret() {
        JwtConfig config = new JwtConfig();
        config.setSecret("too-short");
        MockEnvironment environment = new MockEnvironment();
        environment.setActiveProfiles("local");

        assertThrows(IllegalStateException.class,
                () -> new JwtSecretValidator(config, environment).validate());
    }
}
