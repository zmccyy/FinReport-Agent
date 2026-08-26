package com.finreport.idempotent;

import java.nio.charset.StandardCharsets;
import java.util.UUID;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.core.annotation.Order;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.server.reactive.ServerHttpResponseDecorator;
import org.springframework.stereotype.Component;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.reactive.result.method.annotation.RequestMappingHandlerMapping;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.WebFilter;
import org.springframework.web.server.WebFilterChain;

import com.finreport.trace.TraceContext;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 幂等 WebFilter — M6.04。
 *
 * <p>解析 handler 方法上的 {@link Idempotent} 注解，以
 * {@code Idempotency-Key} 请求头为幂等键：</p>
 * <ul>
 *   <li>缓存命中：直接回放首次响应（status + Content-Type + body）</li>
 *   <li>在途锁冲突（同 Key 请求正在处理）：返回 409</li>
 *   <li>Key 缺失且 required=true：返回 422</li>
 *   <li>首次请求：装饰响应收集 body，完成后写缓存（TTL）并释放锁</li>
 * </ul>
 *
 * <p>Redis 故障时放行（fail-open）：幂等是防重保护而非安全契约，
 * 不应因 Redis 抖动拒绝全部写请求。</p>
 */
@Component
@Order(-85)
public class IdempotentWebFilter implements WebFilter {

    private static final Logger log = LoggerFactory.getLogger(IdempotentWebFilter.class);

    static final String RESP_KEY_PREFIX = "fin:idem:resp:";
    static final String LOCK_KEY_PREFIX = "fin:idem:lock:";
    static final String IDEMPOTENCY_HEADER = "Idempotency-Key";

    /** 在途锁 TTL：覆盖一次写请求的正常处理时长。 */
    private static final long LOCK_TTL_SECONDS = 90;

    private static final String ERROR_TYPE_PREFIX = "https://finreport.example/errors/";

    private final RequestMappingHandlerMapping handlerMapping;
    private final ReactiveRedisTemplate<String, String> redisTemplate;
    private final ObjectMapper objectMapper;

    public IdempotentWebFilter(
            @Qualifier("requestMappingHandlerMapping") RequestMappingHandlerMapping handlerMapping,
            ReactiveRedisTemplate<String, String> redisTemplate,
            ObjectMapper objectMapper) {
        this.handlerMapping = handlerMapping;
        this.redisTemplate = redisTemplate;
        this.objectMapper = objectMapper;
    }

    /** 缓存的响应快照。 */
    record CachedResponse(int status, String contentType, String body) {
    }

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        return handlerMapping.getHandler(exchange)
                .ofType(HandlerMethod.class)
                .mapNotNull(handler -> handler.getMethodAnnotation(Idempotent.class))
                .flatMap(annotation -> handle(exchange, chain, annotation))
                .switchIfEmpty(Mono.defer(() -> chain.filter(exchange).thenReturn(false)))
                .then();
    }

    private Mono<Boolean> handle(ServerWebExchange exchange, WebFilterChain chain, Idempotent annotation) {
        String idempotencyKey = exchange.getRequest().getHeaders().getFirst(IDEMPOTENCY_HEADER);
        if (idempotencyKey == null || idempotencyKey.isBlank()) {
            if (annotation.required()) {
                log.info("[IdempotentWebFilter] 缺少 Idempotency-Key path={}", exchange.getRequest().getPath().value());
                return respondError(exchange, HttpStatus.UNPROCESSABLE_ENTITY,
                        "IDEMPOTENCY_KEY_REQUIRED", "写操作必须携带 Idempotency-Key 请求头")
                        .thenReturn(true);
            }
            return chain.filter(exchange).thenReturn(true);
        }

        String principal = resolvePrincipal(exchange);
        String respKey = RESP_KEY_PREFIX + annotation.name() + ":" + principal + ":" + idempotencyKey;
        String lockKey = LOCK_KEY_PREFIX + annotation.name() + ":" + principal + ":" + idempotencyKey;

        return findCachedResponse(respKey)
                .flatMap(cached -> replay(exchange, cached).thenReturn(true))
                .switchIfEmpty(Mono.defer(() -> acquireLockAndProcess(exchange, chain, annotation, respKey, lockKey)))
                .onErrorResume(e -> {
                    log.warn("[IdempotentWebFilter] Redis 幂等检查失败,放行 key={} error={}",
                            lockKey, e.getMessage());
                    return chain.filter(exchange).thenReturn(true);
                });
    }

    private Mono<Boolean> acquireLockAndProcess(ServerWebExchange exchange, WebFilterChain chain,
                                                Idempotent annotation, String respKey, String lockKey) {
        return redisTemplate.opsForValue()
                .setIfAbsent(lockKey, "1", java.time.Duration.ofSeconds(LOCK_TTL_SECONDS))
                .defaultIfEmpty(false)
                .flatMap(acquired -> {
                    if (!acquired) {
                        log.info("[IdempotentWebFilter] 同 Key 请求处理中,返回 409 key={}", lockKey);
                        return respondError(exchange, HttpStatus.CONFLICT,
                                "IDEMPOTENCY_IN_PROGRESS", "相同 Idempotency-Key 的请求正在处理中，请稍后重试")
                                .thenReturn(true);
                    }
                    return processAndCache(exchange, chain, annotation, respKey, lockKey);
                });
    }

    /** 执行业务链路，收集响应体并写入缓存；无论成败最后释放锁。 */
    private Mono<Boolean> processAndCache(ServerWebExchange exchange, WebFilterChain chain,
                                          Idempotent annotation, String respKey, String lockKey) {
        StringBuilder bodyCollector = new StringBuilder();
        ServerHttpResponseDecorator decorated = new ServerHttpResponseDecorator(exchange.getResponse()) {
            @Override
            public Mono<Void> writeWith(org.reactivestreams.Publisher<? extends org.springframework.core.io.buffer.DataBuffer> body) {
                return super.writeWith(Flux.from(body).doOnNext(buffer -> bodyCollector.append(
                        buffer.toString(buffer.readPosition(), buffer.readableByteCount(), StandardCharsets.UTF_8))));
            }
        };

        return chain.filter(exchange.mutate().response(decorated).build())
                .then(Mono.defer(() -> cacheResponse(respKey, annotation.ttlSeconds(),
                        exchange.getResponse().getStatusCode(),
                        exchange.getResponse().getHeaders().getFirst(HttpHeaders.CONTENT_TYPE),
                        bodyCollector.toString())))
                .thenReturn(true)
                .onErrorResume(e -> Mono.just(true))
                // 无论成功失败（含缓存写入失败）都释放锁；释放本身失败仅记录日志。
                .flatMap(done -> redisTemplate.delete(lockKey)
                        .onErrorResume(e -> {
                            log.warn("[IdempotentWebFilter] 释放幂等锁失败 key={} error={}", lockKey, e.getMessage());
                            return Mono.just(0L);
                        })
                        .thenReturn(done));
    }

    private Mono<Void> cacheResponse(String respKey, int ttlSeconds, org.springframework.http.HttpStatusCode status,
                                     String contentType, String body) {
        if (status == null) {
            return Mono.empty();
        }
        try {
            String payload = objectMapper.writeValueAsString(
                    new CachedResponse(status.value(), contentType, body));
            return redisTemplate.opsForValue()
                    .set(respKey, payload, java.time.Duration.ofSeconds(ttlSeconds))
                    .then();
        } catch (JsonProcessingException e) {
            log.warn("[IdempotentWebFilter] 响应快照序列化失败 key={} error={}", respKey, e.getMessage());
            return Mono.empty();
        }
    }

    private Mono<Void> replay(ServerWebExchange exchange, CachedResponse cached) {
        log.debug("[IdempotentWebFilter] 回放缓存响应 path={} status={}",
                exchange.getRequest().getPath().value(), cached.status());
        exchange.getResponse().setStatusCode(HttpStatus.valueOf(cached.status()));
        if (cached.contentType() != null) {
            exchange.getResponse().getHeaders().set(HttpHeaders.CONTENT_TYPE, cached.contentType());
        }
        exchange.getResponse().getHeaders().set("Idempotency-Replayed", "true");
        byte[] bytes = cached.body() == null ? new byte[0] : cached.body().getBytes(StandardCharsets.UTF_8);
        return exchange.getResponse()
                .writeWith(Mono.just(exchange.getResponse().bufferFactory().wrap(bytes)));
    }

    private Mono<CachedResponse> findCachedResponse(String respKey) {
        return redisTemplate.opsForValue().get(respKey)
                .mapNotNull(payload -> {
                    try {
                        return objectMapper.readValue(payload, CachedResponse.class);
                    } catch (JsonProcessingException e) {
                        log.warn("[IdempotentWebFilter] 缓存响应解析失败 key={} error={}", respKey, e.getMessage());
                        return null;
                    }
                });
    }

    private String resolvePrincipal(ServerWebExchange exchange) {
        String userId = exchange.getRequest().getHeaders().getFirst("X-User-Id");
        if (userId != null && !userId.isBlank()) {
            return "u" + userId;
        }
        java.net.InetSocketAddress remote = exchange.getRequest().getRemoteAddress();
        return "ip" + (remote != null ? remote.getAddress().getHostAddress() : "unknown");
    }

    private Mono<Void> respondError(ServerWebExchange exchange, HttpStatus status, String errorCode, String detail) {
        exchange.getResponse().setStatusCode(status);
        exchange.getResponse().getHeaders().set(HttpHeaders.CONTENT_TYPE, "application/json");
        String traceId = exchange.getAttributeOrDefault(TraceContext.TRACE_ID, "");
        if (traceId == null || traceId.isBlank()) {
            traceId = UUID.randomUUID().toString();
        }
        String body = "{\"type\":\"" + ERROR_TYPE_PREFIX + errorCode + "\""
                + ",\"title\":\"" + status.getReasonPhrase() + "\",\"status\":" + status.value()
                + ",\"detail\":\"" + detail + "\""
                + ",\"instance\":\"" + exchange.getRequest().getPath().value() + "\""
                + ",\"traceId\":\"" + traceId + "\"}";
        return exchange.getResponse()
                .writeWith(Mono.just(exchange.getResponse().bufferFactory()
                        .wrap(body.getBytes(StandardCharsets.UTF_8))));
    }
}
