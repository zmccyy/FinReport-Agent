package com.finreport.ratelimit;

import java.time.Duration;
import java.util.List;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;

import reactor.core.publisher.Mono;

/**
 * Redis 滑动窗口限流器 — spec §2.2 / M6.03。
 *
 * <p>基于 ZSET 滑动窗口 + Lua 原子执行：窗口内请求数未超限则记录本次
 * 时间戳并放行；超限则返回需等待的毫秒数。相比固定窗口计数可避免窗口
 * 边界的瞬时突刺。</p>
 */
@Component
public class RateLimiter {

    private static final Logger log = LoggerFactory.getLogger(RateLimiter.class);

    /**
     * Lua 脚本：返回正数 1 表示放行；返回负数表示超限，其绝对值为建议
     * 重试等待毫秒数（窗口最老一条记录的剩余存活时间）。
     */
    private static final DefaultRedisScript<Long> SLIDING_WINDOW_SCRIPT = new DefaultRedisScript<>(
            """
            local key = KEYS[1]
            local now = tonumber(ARGV[1])
            local window = tonumber(ARGV[2])
            local limit = tonumber(ARGV[3])
            local member = ARGV[4]
            redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
            local count = redis.call('ZCARD', key)
            if count < limit then
              redis.call('ZADD', key, now, member)
              redis.call('PEXPIRE', key, window)
              return 1
            end
            local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
            local retryAfterMs = window - (now - tonumber(oldest[2]))
            if retryAfterMs < 1 then retryAfterMs = 1 end
            return 0 - retryAfterMs
            """,
            Long.class);

    /** 限流判定结果。 */
    public record RateLimitDecision(boolean allowed, long retryAfterMillis) {
        public static RateLimitDecision allow() {
            return new RateLimitDecision(true, 0);
        }

        public static RateLimitDecision reject(long retryAfterMillis) {
            return new RateLimitDecision(false, Math.max(retryAfterMillis, 1));
        }
    }

    private final ReactiveRedisTemplate<String, String> redisTemplate;

    public RateLimiter(ReactiveRedisTemplate<String, String> redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    /**
     * 尝试获取一次限流配额。
     *
     * <p>Redis 故障时放行（fail-open）：限流属于保护性措施而非安全契约，
     * 不应因 Redis 抖动拒绝全部流量；同时记录 WARN 日志供告警。</p>
     *
     * @param key    限流 key（建议格式 fin:rl:{rule}:{principal}）
     * @param limit  窗口内最大请求数
     * @param window 窗口长度
     * @return 判定结果（放行 / 超限 + 建议重试等待毫秒）
     */
    public Mono<RateLimitDecision> tryAcquire(String key, int limit, Duration window) {
        log.debug("[RateLimiter] tryAcquire key={} limit={} window={}ms", key, limit, window.toMillis());
        long now = System.currentTimeMillis();
        long windowMillis = window.toMillis();
        String member = now + "-" + UUID.randomUUID();
        return redisTemplate.execute(SLIDING_WINDOW_SCRIPT,
                        List.of(key),
                        List.of(String.valueOf(now), String.valueOf(windowMillis),
                                String.valueOf(limit), member))
                .next()
                .map(result -> result > 0
                        ? RateLimitDecision.allow()
                        : RateLimitDecision.reject(-result))
                .onErrorResume(e -> {
                    log.warn("[RateLimiter] Redis 限流检查失败,放行 key={} error={}", key, e.getMessage());
                    return Mono.just(RateLimitDecision.allow());
                });
    }
}
