package com.finreport.ratelimit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.when;

import java.time.Duration;
import java.util.List;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.data.redis.core.script.RedisScript;

import reactor.core.publisher.Flux;
import reactor.test.StepVerifier;

/**
 * {@link RateLimiter} 单元测试。
 */
@DisplayName("RateLimiter")
@ExtendWith(MockitoExtension.class)
class RateLimiterTest {

    @Mock
    private ReactiveRedisTemplate<String, String> redisTemplate;

    @Test
    @DisplayName("should allow request when lua script returns positive")
    void shouldAllowRequestWhenLuaScriptReturnsPositive() {
        when(redisTemplate.execute(any(RedisScript.class), anyList(), anyList()))
                .thenReturn(Flux.just(1L));
        RateLimiter limiter = new RateLimiter(redisTemplate);

        StepVerifier.create(limiter.tryAcquire("fin:rl:test:u1", 5, Duration.ofSeconds(60)))
                .assertNext(decision -> {
                    assertTrue(decision.allowed());
                    assertEquals(0, decision.retryAfterMillis());
                })
                .verifyComplete();
    }

    @Test
    @DisplayName("should reject with retry millis when lua script returns negative")
    void shouldRejectWithRetryMillisWhenLuaScriptReturnsNegative() {
        when(redisTemplate.execute(any(RedisScript.class), anyList(), anyList()))
                .thenReturn(Flux.just(-30000L));
        RateLimiter limiter = new RateLimiter(redisTemplate);

        StepVerifier.create(limiter.tryAcquire("fin:rl:test:u1", 5, Duration.ofSeconds(60)))
                .assertNext(decision -> {
                    assertFalse(decision.allowed());
                    assertEquals(30000, decision.retryAfterMillis());
                })
                .verifyComplete();
    }

    @Test
    @DisplayName("should clamp retry millis to at least one when lua returns zero retry")
    void shouldClampRetryMillisToAtLeastOneWhenLuaReturnsZeroRetry() {
        when(redisTemplate.execute(any(RedisScript.class), anyList(), anyList()))
                .thenReturn(Flux.just(0L));
        RateLimiter limiter = new RateLimiter(redisTemplate);

        StepVerifier.create(limiter.tryAcquire("fin:rl:test:u1", 5, Duration.ofSeconds(60)))
                .assertNext(decision -> {
                    assertFalse(decision.allowed());
                    assertEquals(1, decision.retryAfterMillis());
                })
                .verifyComplete();
    }

    @Test
    @DisplayName("should fail open when redis errors")
    void shouldFailOpenWhenRedisErrors() {
        when(redisTemplate.execute(any(RedisScript.class), anyList(), anyList()))
                .thenReturn(Flux.error(new RuntimeException("redis down")));
        RateLimiter limiter = new RateLimiter(redisTemplate);

        StepVerifier.create(limiter.tryAcquire("fin:rl:test:u1", 5, Duration.ofSeconds(60)))
                .assertNext(decision -> assertTrue(decision.allowed()))
                .verifyComplete();
    }

    @Test
    @DisplayName("should pass key and window args to redis script")
    void shouldPassKeyAndWindowArgsToRedisScript() {
        when(redisTemplate.execute(any(RedisScript.class), eq(List.of("fin:rl:r:u1")),
                anyList())).thenReturn(Flux.just(1L));
        RateLimiter limiter = new RateLimiter(redisTemplate);

        StepVerifier.create(limiter.tryAcquire("fin:rl:r:u1", 3, Duration.ofSeconds(60)))
                .assertNext(decision -> assertTrue(decision.allowed()))
                .verifyComplete();
    }
}
