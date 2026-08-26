package com.finreport.ratelimit;

import java.lang.annotation.Documented;
import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * 接口限流注解 — spec §2.2 RateLimiter / M6.03。
 *
 * <p>标注在 Controller 方法（或类）上，由 {@link RateLimitWebFilter} 解析并
 * 按 Redis 滑动窗口限流。超限返回 429 + Retry-After（RFC 9457 错误体）。</p>
 *
 * <p>限流维度与阈值（决策记录 2026-08-26-m6-03-04）：</p>
 * <ul>
 *   <li>登录/注册：5 次/分/IP（认证前无 userId）</li>
 *   <li>上传：3 次/分/用户（spec §3.10 单用户并发约束的入口侧配合）</li>
 *   <li>问答写操作：10 次/分/用户</li>
 * </ul>
 */
@Documented
@Target({ElementType.METHOD, ElementType.TYPE})
@Retention(RetentionPolicy.RUNTIME)
public @interface RateLimit {

    /** 规则名，作为 Redis key 的一部分，需全局唯一。 */
    String name();

    /** 窗口内允许的最大请求数。 */
    int limit() default 10;

    /** 窗口长度（秒）。 */
    int windowSeconds() default 1;

    /** 计数维度：USER（X-User-Id，缺失回退 IP）或 IP。 */
    KeyType key() default KeyType.USER;

    /** 计数维度类型。 */
    enum KeyType {
        /** 按认证用户（JwtFilter 注入的 X-User-Id）计数。 */
        USER,
        /** 按客户端 IP 计数（用于登录/注册等认证前端点）。 */
        IP
    }
}
