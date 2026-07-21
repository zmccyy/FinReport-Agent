package com.finreport.service.orchestrator;

import java.time.Duration;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.stereotype.Component;

import com.finreport.domain.enums.TaskStepName;

import reactor.core.publisher.Mono;

/**
 * 三表抽取完成计数器 — spec §3.2.1 + plan M2.08。
 *
 * <p>用 Redis 原子 INCR 计数 3 条 extract 步骤的成功回报，计数到
 * {@link #EXPECTED_COUNT} 即可立即触发 CHECK 调度，无需逐条 progress 都查
 * MySQL {@code task_step} 表（spec §3.2.1 "AtomicInteger 计数 + Redis 状态机"）。</p>
 *
 * <p>失败回报通过独立的 failed flag key 记录；CHECK 调度条件必须同时满足
 * success count {@code >= EXPECTED_COUNT} 且无 failed flag。Redis 故障时
 * 上层（{@link TaskOrchestrator}）回退到 MySQL {@code checkAllExtractsDone}
 * 兜底路径。</p>
 *
 * <p>键设计：</p>
 * <ul>
 *   <li>{@code fin:extract:done:{taskId}} — INCR 计数 success（首次 INCR 时设 TTL）</li>
 *   <li>{@code fin:extract:failed:{taskId}} — SET 失败标记（任意 step 失败时 SET）</li>
 * </ul>
 *
 * <p>TTL = 1 day：任务最长 8 分钟（spec §12.1），1 天足够保留排查；
 * 任务重新发布（重试或 reconcile 重放）时调用 {@link #reset} 清理。</p>
 */
@Component
public class ExtractCompletionTracker {

    private static final Logger log = LoggerFactory.getLogger(ExtractCompletionTracker.class);

    /** 三表抽取步骤总数（BS / IS / CF）。 */
    public static final int EXPECTED_COUNT = 3;

    /** 计数 key 前缀：{@code fin:extract:done:{taskId}}。 */
    public static final String KEY_DONE_PREFIX = "fin:extract:done:";

    /** 失败标记 key 前缀：{@code fin:extract:failed:{taskId}}。 */
    public static final String KEY_FAILED_PREFIX = "fin:extract:failed:";

    /** key TTL：1 天（任务最长 8 分钟，1 天足够保留排查）。 */
    public static final Duration KEY_TTL = Duration.ofDays(1);

    private final ReactiveRedisTemplate<String, String> redisTemplate;

    public ExtractCompletionTracker(ReactiveRedisTemplate<String, String> redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    /**
     * 记录某个 extract 步骤成功回报，返回当前累计 success 计数。
     *
     * <p>原子 {@code INCR} + 首次 {@code EXPIRE}：仅在 count == 1 时设 TTL，
     * 避免每次 INCR 都刷新 TTL 导致 key 永不过期。</p>
     *
     * @param taskId 任务 ID
     * @param step   抽取步骤（BS / IS / CF），仅用于日志
     * @return 当前累计 success 计数（1~3+）；Redis 故障返回 0 触发上层 MySQL 兜底
     */
    public Mono<Integer> recordSuccess(String taskId, TaskStepName step) {
        log.debug("[ExtractCompletionTracker] recordSuccess taskId={} step={}", taskId, step);
        String key = KEY_DONE_PREFIX + taskId;
        return redisTemplate.opsForValue().increment(key)
                .map(Long::intValue)
                .flatMap(count -> {
                    if (count == 1) {
                        return redisTemplate.expire(key, KEY_TTL).thenReturn(count);
                    }
                    return Mono.just(count);
                })
                .onErrorResume(error -> {
                    log.warn("[ExtractCompletionTracker] recordSuccess Redis 故障 taskId={} step={}",
                            taskId, step, error);
                    return Mono.just(0);
                });
    }

    /**
     * 记录某个 extract 步骤失败回报。
     *
     * <p>SET failed flag + TTL；CHECK 调度条件必须同时满足
     * success count {@code >= EXPECTED_COUNT} 且 failed flag 不存在。
     * 失败 step 进入 {@code RETRY} 后重试 publish 时调用 {@link #clearFailure}
     * 清除 flag，避免重试成功后 CHECK 仍被阻断。</p>
     *
     * @param taskId 任务 ID
     * @param step   抽取步骤
     * @return 完成信号；Redis 故障静默吞掉（上层 MySQL 兜底）
     */
    public Mono<Void> recordFailure(String taskId, TaskStepName step) {
        log.debug("[ExtractCompletionTracker] recordFailure taskId={} step={}", taskId, step);
        String key = KEY_FAILED_PREFIX + taskId;
        return redisTemplate.opsForValue().set(key, step.name(), KEY_TTL)
                .then()
                .onErrorResume(error -> {
                    log.warn("[ExtractCompletionTracker] recordFailure Redis 故障 taskId={} step={}",
                            taskId, step, error);
                    return Mono.empty();
                });
    }

    /**
     * 清除失败标记（用于 step 重试 publish 时）。
     *
     * @param taskId 任务 ID
     * @return 完成信号；Redis 故障静默吞掉
     */
    public Mono<Void> clearFailure(String taskId) {
        log.debug("[ExtractCompletionTracker] clearFailure taskId={}", taskId);
        return redisTemplate.delete(KEY_FAILED_PREFIX + taskId)
                .then()
                .onErrorResume(error -> {
                    log.warn("[ExtractCompletionTracker] clearFailure Redis 故障 taskId={}", taskId, error);
                    return Mono.empty();
                });
    }

    /**
     * 查询当前 success 计数。
     *
     * @param taskId 任务 ID
     * @return 当前计数；key 不存在或 Redis 故障返回 0
     */
    public Mono<Integer> getCount(String taskId) {
        String key = KEY_DONE_PREFIX + taskId;
        return redisTemplate.opsForValue().get(key)
                .map(Integer::parseInt)
                .defaultIfEmpty(0)
                .onErrorResume(error -> {
                    log.warn("[ExtractCompletionTracker] getCount Redis 故障 taskId={}", taskId, error);
                    return Mono.just(0);
                });
    }

    /**
     * 是否已记录失败标记。
     *
     * @param taskId 任务 ID
     * @return {@code true} 如果存在 failed flag；Redis 故障返回 {@code false}
     */
    public Mono<Boolean> hasFailed(String taskId) {
        String key = KEY_FAILED_PREFIX + taskId;
        return redisTemplate.hasKey(key)
                .onErrorResume(error -> {
                    log.warn("[ExtractCompletionTracker] hasFailed Redis 故障 taskId={}", taskId, error);
                    return Mono.just(false);
                });
    }

    /**
     * 清空计数 + 失败标记（用于任务重试或重新发布）。
     *
     * @param taskId 任务 ID
     * @return 完成信号；Redis 故障静默吞掉
     */
    public Mono<Void> reset(String taskId) {
        log.debug("[ExtractCompletionTracker] reset taskId={}", taskId);
        return redisTemplate.delete(KEY_DONE_PREFIX + taskId, KEY_FAILED_PREFIX + taskId)
                .then()
                .onErrorResume(error -> {
                    log.warn("[ExtractCompletionTracker] reset Redis 故障 taskId={}", taskId, error);
                    return Mono.empty();
                });
    }
}
