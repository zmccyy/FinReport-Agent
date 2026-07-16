package com.finreport.security;

import java.util.List;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.annotation.Order;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.server.reactive.ServerHttpRequest;
import org.springframework.stereotype.Component;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.WebFilter;
import org.springframework.web.server.WebFilterChain;

import com.finreport.service.AuthService;

import reactor.core.publisher.Mono;

/**
 * JWT 认证 WebFilter。
 *
 * <p>从 Authorization Header 提取 Bearer Token，校验签名和黑名单后设置用户上下文。
 * 公开路径直接放行；无效 Token 返回 401。</p>
 */
@Component
@Order(-100)
public class JwtFilter implements WebFilter {

    private static final Logger log = LoggerFactory.getLogger(JwtFilter.class);

    private static final String BEARER_PREFIX = "Bearer ";
    private static final List<String> PUBLIC_PATH_PREFIXES = List.of(
            "/api/v1/auth/",
            "/api/v1/system/health",
            "/actuator"
    );

    private final JwtUtil jwtUtil;
    private final AuthService authService;

    public JwtFilter(JwtUtil jwtUtil, AuthService authService) {
        this.jwtUtil = jwtUtil;
        this.authService = authService;
    }

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        String path = exchange.getRequest().getPath().value();

        // 公开路径直接放行
        if (PUBLIC_PATH_PREFIXES.stream().anyMatch(path::startsWith)) {
            return chain.filter(exchange);
        }

        // 提取 Authorization Header
        String authHeader = exchange.getRequest().getHeaders().getFirst(HttpHeaders.AUTHORIZATION);
        if (authHeader == null || !authHeader.startsWith(BEARER_PREFIX)) {
            log.debug("[JwtFilter] 缺少 Authorization Header path={}", path);
            return respond401(exchange, "缺少认证信息");
        }

        String token = authHeader.substring(BEARER_PREFIX.length()).trim();

        // 校验 Token 签名和过期
        if (!jwtUtil.validate(token)) {
            log.debug("[JwtFilter] Token 无效 path={}", path);
            return respond401(exchange, "Token 无效或已过期");
        }

        // 校验 Token 黑名单（Redis 不可用时降级放行）
        String jti = jwtUtil.getJti(token);
        return authService.isBlacklisted(jti)
                .flatMap(isBlacklisted -> {
                    if (Boolean.TRUE.equals(isBlacklisted)) {
                        log.debug("[JwtFilter] Token 已被撤销 jti={} path={}", jti, path);
                        return respond401(exchange, "Token 已被撤销");
                    }
                    return injectUserContext(exchange, chain, token, path);
                })
                .onErrorResume(e -> {
                    log.warn("[JwtFilter] Redis 黑名单检查失败，降级放行 path={} error={}", path, e.getMessage());
                    return injectUserContext(exchange, chain, token, path);
                });
    }

    private Mono<Void> injectUserContext(ServerWebExchange exchange, WebFilterChain chain,
                                          String token, String path) {
        Long userId = jwtUtil.getUserId(token);
        String username = jwtUtil.getUsername(token);

        if (userId == null || username == null) {
            log.warn("[JwtFilter] Token 有效但缺少必要 claims path={}", path);
            return respond401(exchange, "Token 格式异常");
        }

        log.debug("[JwtFilter] 认证成功 userId={} username={} path={}", userId, username, path);

        ServerHttpRequest mutatedRequest = exchange.getRequest().mutate()
                .header("X-User-Id", userId.toString())
                .header("X-Username", username)
                .build();
        return chain.filter(exchange.mutate().request(mutatedRequest).build());
    }

    private Mono<Void> respond401(ServerWebExchange exchange, String message) {
        exchange.getResponse().setStatusCode(HttpStatus.UNAUTHORIZED);
        exchange.getResponse().getHeaders().set(HttpHeaders.CONTENT_TYPE, "application/json");
        byte[] body = ("{\"type\":\"https://finreport.example/errors/UNAUTHORIZED\","
                + "\"title\":\"Unauthorized\",\"status\":401,"
                + "\"detail\":\"" + message + "\"}").getBytes(java.nio.charset.StandardCharsets.UTF_8);
        return exchange.getResponse()
                .writeWith(Mono.just(exchange.getResponse().bufferFactory().wrap(body)));
    }
}
