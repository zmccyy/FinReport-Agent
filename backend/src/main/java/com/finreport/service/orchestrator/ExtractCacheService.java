package com.finreport.service.orchestrator;

import java.time.Duration;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.stereotype.Component;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.finreport.domain.enums.TaskStepName;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 抽取结果缓存服务 — spec §3.10 + plan M2.10。
 *
 * <p>把 L3 extract progress 的 result payload 按 {@code pdf_md5 + step} 缓存到 Redis
 * （TTL 7d）。同 PDF 重传时，{@link TaskOrchestrator#dispatchExtractionSteps}
 * 在调 {@link ExtractDispatcher#dispatchAll} 之前先查缓存：三表全部命中即跳过
 * extract MQ 投递，直接重放 3 条 success 路径并触发 CHECK。</p>
 *
 * <p>键设计：{@code fin:cache:extract:{pdfMd5}:{step.name()}}，例如
 * {@code fin:cache:extract:d41d8cd98f00b204e9800998ecf8427e:EXTRACT_BS}。
 * 跨用户共享 — 同 PDF 内容（同 md5）的 extract 结果对任意用户都成立。</p>
 *
 * <p>失败策略：Redis 故障静默吞掉，{@code lookup} 返回空 Mono、{@code lookupAll}
 * 返回空 Map、{@code store} 返回完成 Mono，不影响主流程；上层 fallback 到
 * {@link ExtractDispatcher#dispatchAll} 走 MQ 投递。</p>
 */
@Component
public class ExtractCacheService {

    private static final Logger log = LoggerFactory.getLogger(ExtractCacheService.class);

    /** key 前缀：{@code fin:cache:extract:}（spec §3.10）。 */
    public static final String KEY_PREFIX = "fin:cache:extract:";

    /** TTL：7 天（spec §3.10）。 */
    public static final Duration TTL = Duration.ofDays(7);

    /** 三表抽取步骤固定顺序（与 ExtractDispatcher.EXTRACTION_STEPS 对称）。 */
    public static final List<TaskStepName> EXTRACTION_STEPS = List.of(
            TaskStepName.EXTRACT_BS,
            TaskStepName.EXTRACT_IS,
            TaskStepName.EXTRACT_CF);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {
    };

    private final ReactiveRedisTemplate<String, String> redisTemplate;
    private final ObjectMapper objectMapper;

    public ExtractCacheService(
            ReactiveRedisTemplate<String, String> redisTemplate,
            ObjectMapper objectMapper) {
        this.redisTemplate = redisTemplate;
        this.objectMapper = objectMapper;
    }

    /**
     * 构造缓存 key。
     *
     * @param pdfMd5 PDF 内容 MD5（32 位小写十六进制）
     * @param step   抽取步骤（BS / IS / CF）
     * @return {@code fin:cache:extract:{pdfMd5}:{step.name()}}
     */
    public static String cacheKey(String pdfMd5, TaskStepName step) {
        return KEY_PREFIX + pdfMd5 + ":" + step.name();
    }

    /**
     * 查询单个步骤的缓存。
     *
     * @param pdfMd5 PDF 内容 MD5
     * @param step   抽取步骤
     * @return 缓存的 result payload；未命中或 Redis 故障返回 empty Mono
     */
    public Mono<Map<String, Object>> lookup(String pdfMd5, TaskStepName step) {
        if (pdfMd5 == null || step == null) {
            return Mono.empty();
        }
        String key = cacheKey(pdfMd5, step);
        return redisTemplate.opsForValue().get(key)
                .flatMap(this::deserialize)
                .onErrorResume(error -> {
                    log.warn("[ExtractCacheService] lookup Redis 故障 pdfMd5={} step={}",
                            pdfMd5, step, error);
                    return Mono.empty();
                });
    }

    /**
     * 查询三表抽取缓存，仅返回存在的 entries。
     *
     * <p>用于 {@link TaskOrchestrator} 判定 "全部命中"（{@code size() == 3}）：
     * 全部命中 → 跳过 extract MQ；任一缺失 → 走 MQ 投递。</p>
     *
     * @param pdfMd5 PDF 内容 MD5
     * @return 已缓存的 entries Map；Redis 故障返回空 Map
     */
    public Mono<Map<TaskStepName, Map<String, Object>>> lookupAll(String pdfMd5) {
        if (pdfMd5 == null) {
            return Mono.just(Map.of());
        }
        return Flux.fromIterable(EXTRACTION_STEPS)
                .concatMap(step -> lookup(pdfMd5, step)
                        .map(value -> Map.entry(step, value)))
                .collectMap(Map.Entry::getKey, Map.Entry::getValue)
                .onErrorResume(error -> {
                    log.warn("[ExtractCacheService] lookupAll Redis 故障 pdfMd5={}", pdfMd5, error);
                    return Mono.just(Map.of());
                });
    }

    /**
     * 写入单个步骤的 extract 缓存。
     *
     * <p>由 {@link TaskOrchestrator#handleStepSuccess} 在 L3 extract progress
     * SUCCESS 后调用；result payload 序列化为 JSON 写入 Redis，TTL=7d。
     * Redis 故障静默吞掉。</p>
     *
     * @param pdfMd5 PDF 内容 MD5
     * @param step   抽取步骤
     * @param result L3 progress 携带的 result payload
     * @return 完成信号；Redis 故障也返回完成
     */
    public Mono<Void> store(String pdfMd5, TaskStepName step, Map<String, Object> result) {
        if (pdfMd5 == null || step == null || result == null) {
            return Mono.empty();
        }
        return Mono.defer(() -> {
            String key = cacheKey(pdfMd5, step);
            String json;
            try {
                json = objectMapper.writeValueAsString(result);
            } catch (Exception error) {
                log.warn("[ExtractCacheService] store JSON 序列化失败 pdfMd5={} step={}",
                        pdfMd5, step, error);
                return Mono.<Void>empty();
            }
            return redisTemplate.opsForValue().set(key, json, TTL)
                    .<Void>then()
                    .onErrorResume(error -> {
                        log.warn("[ExtractCacheService] store Redis 故障 pdfMd5={} step={}",
                                pdfMd5, step, error);
                        return Mono.empty();
                    });
        });
    }

    private Mono<Map<String, Object>> deserialize(String json) {
        return Mono.fromCallable(() -> objectMapper.readValue(json, MAP_TYPE))
                .onErrorResume(error -> {
                    log.warn("[ExtractCacheService] 反序列化失败 json={}", json, error);
                    return Mono.empty();
                });
    }
}
