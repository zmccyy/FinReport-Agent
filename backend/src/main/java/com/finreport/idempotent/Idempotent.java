package com.finreport.idempotent;

import java.lang.annotation.Documented;
import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * 写操作幂等注解 — M6.04。
 *
 * <p>标注在 Controller 方法上，由 {@link IdempotentWebFilter} 解析：
 * 携带相同 {@code Idempotency-Key} 的重复请求直接回放缓存的首次响应
 * （状态码 + Content-Type + 响应体），不再进入业务链路。</p>
 *
 * <p>覆盖范围（决策记录 2026-08-26-m6-03-04）：问答会话创建等 HTTP 写端点。
 * 财报上传已有业务级幂等（FileService 按 Key + md5 去重，见其 Javadoc），
 * 不叠加 HTTP 层回放，避免双层幂等语义冲突；SSE 端点响应体不可缓存，
 * 天然不适用。</p>
 */
@Documented
@Target(ElementType.METHOD)
@Retention(RetentionPolicy.RUNTIME)
public @interface Idempotent {

    /** 规则名，作为 Redis key 的一部分，需全局唯一。 */
    String name();

    /** 响应缓存 TTL（秒），默认 24 小时。 */
    int ttlSeconds() default 24 * 60 * 60;

    /** 是否强制要求 Idempotency-Key：true 时缺失返回 422。 */
    boolean required() default true;
}
