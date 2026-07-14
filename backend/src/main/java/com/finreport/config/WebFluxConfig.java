package com.finreport.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.reactive.config.CorsRegistry;
import org.springframework.web.reactive.config.WebFluxConfigurer;

// 注：不启用 @EnableWebFlux，由 Spring Boot 自动配置接管。
// 仅实现 WebFluxConfigurer 追加自定义设置，避免覆盖 Boot 的默认 WebFlux 配置。
@Configuration
public class WebFluxConfig implements WebFluxConfigurer {

    /** CORS 预检缓存时间（秒） */
    private static final long CORS_MAX_AGE_SECONDS = 3600;

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/api/**")
                .allowedOriginPatterns("*")
                .allowedMethods("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS")
                .allowedHeaders("*")
                .allowCredentials(true)
                .maxAge(CORS_MAX_AGE_SECONDS);
    }
}
