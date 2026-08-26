package com.finreport.idempotent;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.contains;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Duration;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.data.redis.core.ReactiveValueOperations;
import org.springframework.http.HttpStatus;
import org.springframework.mock.http.server.reactive.MockServerHttpRequest;
import org.springframework.mock.web.server.MockServerWebExchange;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.reactive.result.method.annotation.RequestMappingHandlerMapping;
import org.springframework.web.server.WebFilterChain;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.finreport.idempotent.IdempotentWebFilter.CachedResponse;

import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * {@link IdempotentWebFilter} 单元测试。
 */
@DisplayName("IdempotentWebFilter")
@ExtendWith(MockitoExtension.class)
class IdempotentWebFilterTest {

    /** 测试用 controller：带 @Idempotent 的 handler。 */
    static class TestController {
        @Idempotent(name = "test-write", ttlSeconds = 3600)
        public Mono<Void> write() {
            return Mono.empty();
        }

        public Mono<Void> plain() {
            return Mono.empty();
        }
    }

    @Mock
    private RequestMappingHandlerMapping handlerMapping;

    @Mock
    private ReactiveRedisTemplate<String, String> redisTemplate;

    @Mock
    private ReactiveValueOperations<String, String> valueOps;

    @Mock
    private WebFilterChain chain;

    private final TestController controller = new TestController();
    private IdempotentWebFilter filter;

    @BeforeEach
    void setUp() {
        filter = new IdempotentWebFilter(handlerMapping, redisTemplate, new ObjectMapper());
    }

    private MockServerWebExchange exchangeWithKey(String idempotencyKey) {
        MockServerHttpRequest request = MockServerHttpRequest.post("/api/v1/test")
                .header("X-User-Id", "42")
                .header("Idempotency-Key", idempotencyKey)
                .build();
        return MockServerWebExchange.from(request);
    }

    private HandlerMethod handlerFor(String methodName) throws NoSuchMethodException {
        return new HandlerMethod(controller, TestController.class.getMethod(methodName));
    }

    @Test
    @DisplayName("should return 422 when idempotency key missing and required")
    void shouldReturn422WhenIdempotencyKeyMissingAndRequired() throws NoSuchMethodException {
        MockServerWebExchange exchange = MockServerWebExchange.from(
                MockServerHttpRequest.post("/api/v1/test").header("X-User-Id", "42").build());
        when(handlerMapping.getHandler(exchange)).thenReturn(Mono.just(handlerFor("write")));

        StepVerifier.create(filter.filter(exchange, chain)).verifyComplete();
        assertEquals(HttpStatus.UNPROCESSABLE_ENTITY, exchange.getResponse().getStatusCode());
    }

    @Test
    @DisplayName("should replay cached response without invoking chain")
    void shouldReplayCachedResponseWithoutInvokingChain() throws NoSuchMethodException {
        MockServerWebExchange exchange = exchangeWithKey("key-1");
        when(handlerMapping.getHandler(exchange)).thenReturn(Mono.just(handlerFor("write")));
        when(redisTemplate.opsForValue()).thenReturn(valueOps);
        when(valueOps.get(contains("fin:idem:resp:test-write:u42:key-1")))
                .thenReturn(Mono.just("{\"status\":201,\"contentType\":\"application/json\",\"body\":\"{\\\"id\\\":1}\"}"));

        StepVerifier.create(filter.filter(exchange, chain)).verifyComplete();
        assertEquals(HttpStatus.CREATED, exchange.getResponse().getStatusCode());
        assertEquals("true", exchange.getResponse().getHeaders().getFirst("Idempotency-Replayed"));
    }

    @Test
    @DisplayName("should return 409 when same key request is in flight")
    void shouldReturn409WhenSameKeyRequestIsInFlight() throws NoSuchMethodException {
        MockServerWebExchange exchange = exchangeWithKey("key-2");
        when(handlerMapping.getHandler(exchange)).thenReturn(Mono.just(handlerFor("write")));
        when(redisTemplate.opsForValue()).thenReturn(valueOps);
        when(valueOps.get(anyString())).thenReturn(Mono.empty());
        when(valueOps.setIfAbsent(contains("fin:idem:lock:test-write:u42:key-2"), anyString(),
                any(Duration.class))).thenReturn(Mono.just(false));

        StepVerifier.create(filter.filter(exchange, chain)).verifyComplete();
        assertEquals(HttpStatus.CONFLICT, exchange.getResponse().getStatusCode());
    }

    @Test
    @DisplayName("should process first request and cache response then release lock")
    void shouldProcessFirstRequestAndCacheResponseThenReleaseLock() throws NoSuchMethodException {
        MockServerWebExchange exchange = exchangeWithKey("key-3");
        when(handlerMapping.getHandler(exchange)).thenReturn(Mono.just(handlerFor("write")));
        when(redisTemplate.opsForValue()).thenReturn(valueOps);
        when(valueOps.get(anyString())).thenReturn(Mono.empty());
        when(valueOps.setIfAbsent(contains("lock"), anyString(), any(Duration.class)))
                .thenReturn(Mono.just(true));
        when(valueOps.set(anyString(), anyString(), any(Duration.class))).thenReturn(Mono.just(true));
        when(redisTemplate.delete(anyString())).thenReturn(Mono.just(1L));

        // chain 写一段 JSON 响应体，模拟 controller 行为（经 mutate 后的 exchange，
        // 其 response 是收集 body 的 decorator）
        when(chain.filter(any(org.springframework.web.server.ServerWebExchange.class)))
                .thenAnswer(invocation -> {
                    org.springframework.web.server.ServerWebExchange target = invocation.getArgument(0);
                    target.getResponse().setStatusCode(HttpStatus.CREATED);
                    target.getResponse().getHeaders().set(
                            org.springframework.http.HttpHeaders.CONTENT_TYPE, "application/json");
                    byte[] bytes = "{\"id\":1}".getBytes();
                    return target.getResponse().writeWith(
                            Mono.just(target.getResponse().bufferFactory().wrap(bytes)));
                });

        StepVerifier.create(filter.filter(exchange, chain)).verifyComplete();
        verify(valueOps).set(contains("fin:idem:resp:test-write:u42:key-3"), contains("\"status\":201"), any(Duration.class));
        verify(redisTemplate).delete(contains("fin:idem:lock:test-write:u42:key-3"));
        assertEquals(HttpStatus.CREATED, exchange.getResponse().getStatusCode());
    }

    @Test
    @DisplayName("should skip idempotency when handler has no annotation")
    void shouldSkipIdempotencyWhenHandlerHasNoAnnotation() throws NoSuchMethodException {
        MockServerWebExchange exchange = MockServerWebExchange.from(MockServerHttpRequest.get("/api/v1/other").build());
        when(handlerMapping.getHandler(exchange)).thenReturn(Mono.just(handlerFor("plain")));
        when(chain.filter(exchange)).thenReturn(Mono.empty());

        StepVerifier.create(filter.filter(exchange, chain)).verifyComplete();
        verify(chain).filter(exchange);
    }

    @Test
    @DisplayName("should fail open when redis errors")
    void shouldFailOpenWhenRedisErrors() throws NoSuchMethodException {
        MockServerWebExchange exchange = exchangeWithKey("key-4");
        when(handlerMapping.getHandler(exchange)).thenReturn(Mono.just(handlerFor("write")));
        when(redisTemplate.opsForValue()).thenReturn(valueOps);
        when(valueOps.get(anyString())).thenReturn(Mono.error(new RuntimeException("redis down")));
        when(chain.filter(any(org.springframework.web.server.ServerWebExchange.class))).thenReturn(Mono.empty());

        StepVerifier.create(filter.filter(exchange, chain)).verifyComplete();
        verify(chain).filter(any(org.springframework.web.server.ServerWebExchange.class));
    }
}
