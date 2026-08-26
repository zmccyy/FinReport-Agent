package com.finreport.ratelimit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.contains;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.net.InetSocketAddress;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicBoolean;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.HttpStatus;
import org.springframework.mock.http.server.reactive.MockServerHttpRequest;
import org.springframework.mock.web.server.MockServerWebExchange;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.reactive.result.method.annotation.RequestMappingHandlerMapping;
import org.springframework.web.server.WebFilterChain;

import com.finreport.ratelimit.RateLimiter.RateLimitDecision;
import com.finreport.ratelimit.RateLimit.KeyType;

import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * {@link RateLimitWebFilter} 单元测试。
 */
@DisplayName("RateLimitWebFilter")
@ExtendWith(MockitoExtension.class)
class RateLimitWebFilterTest {

    /** 测试用 controller：提供带注解与不带注解的 handler 方法。 */
    static class TestController {
        @RateLimit(name = "test-limit", limit = 5, windowSeconds = 60, key = KeyType.IP)
        public Mono<Void> limited() {
            return Mono.empty();
        }

        public Mono<Void> unlimited() {
            return Mono.empty();
        }
    }

    @Mock
    private RequestMappingHandlerMapping handlerMapping;

    @Mock
    private RateLimiter rateLimiter;

    @Mock
    private WebFilterChain chain;

    private final TestController controller = new TestController();
    private RateLimitWebFilter filter;

    @BeforeEach
    void setUp() {
        filter = new RateLimitWebFilter(handlerMapping, rateLimiter);
    }

    private HandlerMethod handlerFor(String methodName) throws NoSuchMethodException {
        return new HandlerMethod(controller, TestController.class.getMethod(methodName));
    }

    @Test
    @DisplayName("should forward request when rate limit allows")
    void shouldForwardRequestWhenRateLimitAllows() throws NoSuchMethodException {
        MockServerWebExchange exchange = MockServerWebExchange.from(
                MockServerHttpRequest.post("/api/v1/test").remoteAddress(new InetSocketAddress("10.0.0.1", 1234)).build());
        when(handlerMapping.getHandler(exchange)).thenReturn(Mono.just(handlerFor("limited")));
        when(rateLimiter.tryAcquire(contains("test-limit:ip10.0.0.1"), eq(5), eq(Duration.ofSeconds(60))))
                .thenReturn(Mono.just(RateLimitDecision.allow()));
        when(chain.filter(exchange)).thenReturn(Mono.empty());

        StepVerifier.create(filter.filter(exchange, chain)).verifyComplete();
        verify(chain).filter(exchange);
    }

    @Test
    @DisplayName("should return 429 with retry after when rate limit rejected")
    void shouldReturn429WithRetryAfterWhenRateLimitRejected() throws NoSuchMethodException {
        MockServerWebExchange exchange = MockServerWebExchange.from(
                MockServerHttpRequest.post("/api/v1/test").remoteAddress(new InetSocketAddress("10.0.0.1", 1234)).build());
        when(handlerMapping.getHandler(exchange)).thenReturn(Mono.just(handlerFor("limited")));
        when(rateLimiter.tryAcquire(contains("test-limit:ip10.0.0.1"), eq(5), eq(Duration.ofSeconds(60))))
                .thenReturn(Mono.just(RateLimitDecision.reject(30000)));

        StepVerifier.create(filter.filter(exchange, chain)).verifyComplete();
        assertEquals(HttpStatus.TOO_MANY_REQUESTS, exchange.getResponse().getStatusCode());
        assertEquals("30", exchange.getResponse().getHeaders().getFirst("Retry-After"));
    }

    @Test
    @DisplayName("should skip rate limit when handler has no annotation")
    void shouldSkipRateLimitWhenHandlerHasNoAnnotation() throws NoSuchMethodException {
        MockServerWebExchange exchange = MockServerWebExchange.from(MockServerHttpRequest.get("/api/v1/other").build());
        when(handlerMapping.getHandler(exchange)).thenReturn(Mono.just(handlerFor("unlimited")));
        when(chain.filter(exchange)).thenReturn(Mono.empty());

        StepVerifier.create(filter.filter(exchange, chain)).verifyComplete();
        verify(chain).filter(exchange);
    }

    @Test
    @DisplayName("should skip rate limit when no handler matched")
    void shouldSkipRateLimitWhenNoHandlerMatched() {
        MockServerWebExchange exchange = MockServerWebExchange.from(MockServerHttpRequest.get("/not-an-api").build());
        when(handlerMapping.getHandler(exchange)).thenReturn(Mono.empty());
        when(chain.filter(exchange)).thenReturn(Mono.empty());

        StepVerifier.create(filter.filter(exchange, chain)).verifyComplete();
        verify(chain).filter(exchange);
    }

    @Test
    @DisplayName("should use user id as principal when key type is user")
    void shouldUseUserIdAsPrincipalWhenKeyTypeIsUser() throws NoSuchMethodException {
        HandlerMethod userLimited = new HandlerMethod(new UserLimitedController(),
                UserLimitedController.class.getMethod("limited"));
        MockServerWebExchange exchange = MockServerWebExchange.from(
                MockServerHttpRequest.post("/api/v1/upload").header("X-User-Id", "42").build());
        when(handlerMapping.getHandler(exchange)).thenReturn(Mono.just(userLimited));
        when(rateLimiter.tryAcquire(contains("upload:u42"), eq(3), eq(Duration.ofSeconds(60))))
                .thenReturn(Mono.just(RateLimitDecision.allow()));
        when(chain.filter(exchange)).thenReturn(Mono.empty());

        StepVerifier.create(filter.filter(exchange, chain)).verifyComplete();
        assertTrue(exchange.getResponse().getStatusCode() == null
                || exchange.getResponse().getStatusCode().is2xxSuccessful());
    }

    /** USER 维度注解的测试 controller。 */
    static class UserLimitedController {
        @RateLimit(name = "upload", limit = 3, windowSeconds = 60, key = KeyType.USER)
        public Mono<Void> limited() {
            return Mono.empty();
        }
    }
}
