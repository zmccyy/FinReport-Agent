package com.finreport.ratelimit;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.annotation.Order;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.reactive.result.method.annotation.RequestMappingHandlerMapping;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.WebFilter;
import org.springframework.web.server.WebFilterChain;

import com.finreport.trace.TraceContext;

import reactor.core.publisher.Mono;

/**
 * 限流 WebFilter — M6.03。
 *
 * <p>解析目标 handler 方法/类上的 {@link RateLimit} 注解，按注解维度
 * （用户/IP）调用 {@link RateLimiter} 判定；超限直接写 429 + Retry-After
 * 响应（RFC 9457 Problem Details），不再进入业务链路。</p>
 *
 * <p>Order(-90)：位于 JwtFilter(-100) 之后，认证端点外的接口可直接
 * 读取 JwtFilter 注入的 X-User-Id。</p>
 */
@Component
@Order(-90)
public class RateLimitWebFilter implements WebFilter {

    private static final Logger log = LoggerFactory.getLogger(RateLimitWebFilter.class);

    /** Redis key 前缀（与会话/任务缓存命名空间区分）。 */
    static final String KEY_PREFIX = "fin:rl:";

    private static final String ERROR_TYPE_PREFIX = "https://finreport.example/errors/";

    private final RequestMappingHandlerMapping handlerMapping;
    private final RateLimiter rateLimiter;

    public RateLimitWebFilter(RequestMappingHandlerMapping handlerMapping, RateLimiter rateLimiter) {
        this.handlerMapping = handlerMapping;
        this.rateLimiter = rateLimiter;
    }

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        // 有注解 → checkAndForward 全权处理（放行或 429）；无注解/无 handler → 直接放行。
        // flatMap 返回非空标记值，避免 Mono<Void> 完成后的 empty 误触发 switchIfEmpty 重复执行 chain。
        return handlerMapping.getHandler(exchange)
                .ofType(HandlerMethod.class)
                .mapNotNull(this::resolveAnnotation)
                .flatMap(annotation -> checkAndForward(exchange, chain, annotation).thenReturn(true))
                .switchIfEmpty(Mono.defer(() -> chain.filter(exchange).thenReturn(false)))
                .then();
    }

    private Mono<Void> checkAndForward(ServerWebExchange exchange, WebFilterChain chain, RateLimit annotation) {
        String principal = resolvePrincipal(exchange, annotation.key());
        String key = KEY_PREFIX + annotation.name() + ":" + principal;
        return rateLimiter.tryAcquire(key, annotation.limit(), Duration.ofSeconds(annotation.windowSeconds()))
                .flatMap(decision -> {
                    if (decision.allowed()) {
                        return chain.filter(exchange);
                    }
                    log.info("[RateLimitWebFilter] 限流触发 key={} limit={}/{}s retryAfter={}ms",
                            key, annotation.limit(), annotation.windowSeconds(), decision.retryAfterMillis());
                    return respond429(exchange, annotation, decision.retryAfterMillis());
                });
    }

    private RateLimit resolveAnnotation(HandlerMethod handlerMethod) {
        RateLimit methodAnnotation = handlerMethod.getMethodAnnotation(RateLimit.class);
        if (methodAnnotation != null) {
            return methodAnnotation;
        }
        return handlerMethod.getBeanType().getAnnotation(RateLimit.class);
    }

    private String resolvePrincipal(ServerWebExchange exchange, RateLimit.KeyType keyType) {
        if (keyType == RateLimit.KeyType.USER) {
            String userId = exchange.getRequest().getHeaders().getFirst("X-User-Id");
            if (userId != null && !userId.isBlank()) {
                return "u" + userId;
            }
        }
        return "ip" + resolveClientIp(exchange);
    }

    private String resolveClientIp(ServerWebExchange exchange) {
        // docker compose 直连部署无反代，优先读 X-Forwarded-For 仅为前置代理场景预留。
        String forwarded = exchange.getRequest().getHeaders().getFirst("X-Forwarded-For");
        if (forwarded != null && !forwarded.isBlank()) {
            return forwarded.split(",")[0].trim();
        }
        InetSocketAddress remote = exchange.getRequest().getRemoteAddress();
        return remote != null ? remote.getAddress().getHostAddress() : "unknown";
    }

    private Mono<Void> respond429(ServerWebExchange exchange, RateLimit annotation, long retryAfterMillis) {
        long retryAfterSeconds = (retryAfterMillis + 999) / 1000;
        exchange.getResponse().setStatusCode(HttpStatus.TOO_MANY_REQUESTS);
        exchange.getResponse().getHeaders().set("Retry-After", String.valueOf(retryAfterSeconds));
        exchange.getResponse().getHeaders().set(HttpHeaders.CONTENT_TYPE, "application/json");

        String traceId = exchange.getAttributeOrDefault(TraceContext.TRACE_ID, "");
        if (traceId == null || traceId.isBlank()) {
            traceId = java.util.UUID.randomUUID().toString();
        }
        String body = "{\"type\":\"" + ERROR_TYPE_PREFIX + "RATE_LIMITED\""
                + ",\"title\":\"Too Many Requests\",\"status\":429"
                + ",\"detail\":\"请求过于频繁，请在 " + retryAfterSeconds + " 秒后重试\""
                + ",\"instance\":\"" + exchange.getRequest().getPath().value() + "\""
                + ",\"traceId\":\"" + traceId + "\"}";
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        return exchange.getResponse()
                .writeWith(Mono.just(exchange.getResponse().bufferFactory().wrap(bytes)));
    }
}
