package com.finreport.security;

import java.util.List;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.server.reactive.ServerHttpRequest;
import org.springframework.stereotype.Component;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.WebFilter;
import org.springframework.web.server.WebFilterChain;

import reactor.core.publisher.Mono;

/**
 * JWT 认证 WebFilter。
 *
 * <p>从 Authorization Header 提取 Bearer Token，校验签名后设置用户上下文。
 * M1.07 阶段：注册 Filter 但放行所有请求（SecurityConfig permitAll），
 * M1.08 将收紧路径保护。</p>
 */
@Component
public class JwtFilter implements WebFilter {

    private static final Logger log = LoggerFactory.getLogger(JwtFilter.class);

    private static final String BEARER_PREFIX = "Bearer ";
    private static final List<String> PUBLIC_PATHS = List.of(
            "/api/v1/auth/login",
            "/api/v1/auth/register",
            "/api/v1/system/health",
            "/actuator"
    );

    private final JwtUtil jwtUtil;

    public JwtFilter(JwtUtil jwtUtil) {
        this.jwtUtil = jwtUtil;
    }

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        String path = exchange.getRequest().getPath().value();

        // 公开路径直接放行
        if (PUBLIC_PATHS.stream().anyMatch(path::startsWith)) {
            return chain.filter(exchange);
        }

        String authHeader = exchange.getRequest().getHeaders().getFirst(HttpHeaders.AUTHORIZATION);
        if (authHeader == null || !authHeader.startsWith(BEARER_PREFIX)) {
            // M1.07: SecurityConfig permitAll，不在此处拦截
            return chain.filter(exchange);
        }

        String token = authHeader.substring(BEARER_PREFIX.length());
        if (jwtUtil.validate(token)) {
            Long userId = jwtUtil.getUserId(token);
            String username = jwtUtil.getUsername(token);
            log.debug("[JwtFilter] 用户认证成功 userId={} username={}", userId, username);

            // 注入用户信息到请求属性
            ServerHttpRequest mutatedRequest = exchange.getRequest().mutate()
                    .header("X-User-Id", userId.toString())
                    .header("X-Username", username)
                    .build();
            return chain.filter(exchange.mutate().request(mutatedRequest).build());
        }

        // Token 无效但 M1.07 放行；M1.08 改为返回 401
        log.debug("[JwtFilter] Token 无效，M1.07 阶段放行");
        return chain.filter(exchange);
    }
}
