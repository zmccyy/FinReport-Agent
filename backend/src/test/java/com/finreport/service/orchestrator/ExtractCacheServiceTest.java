package com.finreport.service.orchestrator;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.data.redis.core.ReactiveValueOperations;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.finreport.domain.enums.TaskStepName;

import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * ExtractCacheService 单元测试 — M2.10。
 *
 * <p>用 Mockito mock {@link ReactiveRedisTemplate} + {@link ReactiveValueOperations}，
 * 不需要真实 Redis。覆盖：</p>
 * <ul>
 *   <li>cacheKey 格式：{@code fin:cache:extract:{pdfMd5}:{step.name()}}</li>
 *   <li>lookup：命中返回 parsed Map / 未命中返回 empty Mono / Redis 故障返回 empty</li>
 *   <li>lookupAll：返回存在的 entries / 全部未命中返回空 Map / Redis 故障返回空 Map</li>
 *   <li>store：写入 JSON 字符串 + TTL=7d</li>
 *   <li>store：Redis 故障静默吞掉（不抛异常）</li>
 * </ul>
 */
@ExtendWith(MockitoExtension.class)
@DisplayName("ExtractCacheService")
class ExtractCacheServiceTest {

    @Mock
    private ReactiveRedisTemplate<String, String> redisTemplate;

    @Mock
    private ReactiveValueOperations<String, String> valueOps;

    private ExtractCacheService cacheService;

    @BeforeEach
    void setUp() {
        lenient().when(redisTemplate.opsForValue()).thenReturn(valueOps);
        cacheService = new ExtractCacheService(redisTemplate, new ObjectMapper());
    }

    private static Map<String, Object> sampleResult() {
        Map<String, Object> item = new LinkedHashMap<>();
        item.put("item", "货币资金");
        item.put("value", 1.23e9);
        item.put("scope", "合并");
        item.put("period", "本期");

        Map<String, Object> statement = new LinkedHashMap<>();
        statement.put("report_period", "2024-12-31");
        statement.put("currency", "CNY");
        statement.put("unit", "元");
        statement.put("statements", Map.of("balance_sheet", List.of(item)));

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("success", true);
        result.put("statement", statement);
        result.put("confidence", 0.92);
        result.put("source_page", 5);
        return result;
    }

    @Nested
    @DisplayName("cacheKey")
    class CacheKey {

        @Test
        @DisplayName("should format key as fin:cache:extract:{pdfMd5}:{step.name()}")
        void shouldFormatKeyAsPrefixPlusMd5PlusStep() {
            String key = ExtractCacheService.cacheKey("abc123", TaskStepName.EXTRACT_BS);
            assertEquals("fin:cache:extract:abc123:EXTRACT_BS", key);
        }

        @Test
        @DisplayName("should expose KEY_PREFIX and TTL constants")
        void shouldExposeConstants() {
            assertEquals("fin:cache:extract:", ExtractCacheService.KEY_PREFIX);
            assertEquals(Duration.ofDays(7), ExtractCacheService.TTL);
        }
    }

    @Nested
    @DisplayName("lookup")
    class Lookup {

        @Test
        @DisplayName("should return parsed Map when key exists")
        void shouldReturnParsedMapWhenKeyExists() {
            String pdfMd5 = "md5-hit";
            String key = ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_BS);
            String json = "{\"success\":true,\"confidence\":0.92}";
            when(valueOps.get(key)).thenReturn(Mono.just(json));

            StepVerifier.create(cacheService.lookup(pdfMd5, TaskStepName.EXTRACT_BS))
                    .assertNext(result -> {
                        assertTrue(result.containsKey("success"));
                        assertEquals(Boolean.TRUE, result.get("success"));
                        assertEquals(0.92, (Double) result.get("confidence"), 0.0001);
                    })
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return empty Mono when key missing")
        void shouldReturnEmptyMonoWhenKeyMissing() {
            String pdfMd5 = "md5-missing";
            String key = ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_BS);
            when(valueOps.get(key)).thenReturn(Mono.empty());

            StepVerifier.create(cacheService.lookup(pdfMd5, TaskStepName.EXTRACT_BS))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return empty Mono on Redis error")
        void shouldReturnEmptyMonoOnRedisError() {
            String pdfMd5 = "md5-redis-down";
            String key = ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_BS);
            when(valueOps.get(key)).thenReturn(Mono.error(new RuntimeException("connection refused")));

            StepVerifier.create(cacheService.lookup(pdfMd5, TaskStepName.EXTRACT_BS))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return empty Mono on malformed JSON")
        void shouldReturnEmptyMonoOnMalformedJson() {
            String pdfMd5 = "md5-bad-json";
            String key = ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_BS);
            when(valueOps.get(key)).thenReturn(Mono.just("not a json"));

            StepVerifier.create(cacheService.lookup(pdfMd5, TaskStepName.EXTRACT_BS))
                    .verifyComplete();
        }
    }

    @Nested
    @DisplayName("lookupAll")
    class LookupAll {

        @Test
        @DisplayName("should return all 3 entries when all cached")
        void shouldReturnAllThreeEntriesWhenAllCached() {
            String pdfMd5 = "md5-all";
            when(valueOps.get(ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_BS)))
                    .thenReturn(Mono.just("{\"step\":\"bs\"}"));
            when(valueOps.get(ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_IS)))
                    .thenReturn(Mono.just("{\"step\":\"is\"}"));
            when(valueOps.get(ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_CF)))
                    .thenReturn(Mono.just("{\"step\":\"cf\"}"));

            StepVerifier.create(cacheService.lookupAll(pdfMd5))
                    .assertNext(entries -> {
                        assertEquals(3, entries.size());
                        assertTrue(entries.containsKey(TaskStepName.EXTRACT_BS));
                        assertTrue(entries.containsKey(TaskStepName.EXTRACT_IS));
                        assertTrue(entries.containsKey(TaskStepName.EXTRACT_CF));
                    })
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return only existing entries when partial cache")
        void shouldReturnOnlyExistingEntriesWhenPartialCache() {
            String pdfMd5 = "md5-partial";
            when(valueOps.get(ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_BS)))
                    .thenReturn(Mono.just("{\"step\":\"bs\"}"));
            when(valueOps.get(ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_IS)))
                    .thenReturn(Mono.empty());
            when(valueOps.get(ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_CF)))
                    .thenReturn(Mono.just("{\"step\":\"cf\"}"));

            StepVerifier.create(cacheService.lookupAll(pdfMd5))
                    .assertNext(entries -> {
                        assertEquals(2, entries.size());
                        assertTrue(entries.containsKey(TaskStepName.EXTRACT_BS));
                        assertTrue(entries.containsKey(TaskStepName.EXTRACT_CF));
                    })
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return empty map when no entries cached")
        void shouldReturnEmptyMapWhenNoEntriesCached() {
            String pdfMd5 = "md5-none";
            when(valueOps.get(anyString())).thenReturn(Mono.empty());

            StepVerifier.create(cacheService.lookupAll(pdfMd5))
                    .assertNext(entries -> assertTrue(entries.isEmpty()))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should return empty map on Redis error")
        void shouldReturnEmptyMapOnRedisError() {
            String pdfMd5 = "md5-all-down";
            when(valueOps.get(anyString())).thenReturn(Mono.error(new RuntimeException("connection refused")));

            StepVerifier.create(cacheService.lookupAll(pdfMd5))
                    .assertNext(entries -> assertTrue(entries.isEmpty()))
                    .verifyComplete();
        }
    }

    @Nested
    @DisplayName("store")
    class Store {

        @Test
        @DisplayName("should write JSON string with TTL")
        void shouldWriteJsonStringWithTtl() {
            String pdfMd5 = "md5-store";
            String key = ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_BS);
            when(valueOps.set(eq(key), anyString(), eq(ExtractCacheService.TTL)))
                    .thenReturn(Mono.just(true));

            Map<String, Object> result = sampleResult();

            StepVerifier.create(cacheService.store(pdfMd5, TaskStepName.EXTRACT_BS, result))
                    .verifyComplete();

            verify(valueOps).set(eq(key), anyString(), eq(ExtractCacheService.TTL));
        }

        @Test
        @DisplayName("should serialize result payload as JSON")
        void shouldSerializeResultPayloadAsJson() {
            String pdfMd5 = "md5-serialize";
            String key = ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_BS);
            org.mockito.ArgumentCaptor<String> captor = org.mockito.ArgumentCaptor.forClass(String.class);
            when(valueOps.set(eq(key), captor.capture(), eq(ExtractCacheService.TTL)))
                    .thenReturn(Mono.just(true));

            Map<String, Object> result = sampleResult();

            StepVerifier.create(cacheService.store(pdfMd5, TaskStepName.EXTRACT_BS, result))
                    .verifyComplete();

            String json = captor.getValue();
            assertTrue(json.contains("\"success\":true"));
            assertTrue(json.contains("\"confidence\":0.92"));
            assertTrue(json.contains("\"货币资金\""));
        }

        @Test
        @DisplayName("should swallow Redis errors silently")
        void shouldSwallowRedisErrorsSilently() {
            String pdfMd5 = "md5-store-down";
            String key = ExtractCacheService.cacheKey(pdfMd5, TaskStepName.EXTRACT_BS);
            when(valueOps.set(eq(key), anyString(), eq(ExtractCacheService.TTL)))
                    .thenReturn(Mono.error(new RuntimeException("timeout")));

            StepVerifier.create(cacheService.store(pdfMd5, TaskStepName.EXTRACT_BS, sampleResult()))
                    .verifyComplete();
        }

        @Test
        @DisplayName("should not throw when result is null")
        void shouldNotThrowWhenResultIsNull() {
            String pdfMd5 = "md5-store-null";

            StepVerifier.create(cacheService.store(pdfMd5, TaskStepName.EXTRACT_BS, null))
                    .verifyComplete();

            verify(valueOps, never()).set(anyString(), anyString(), any(Duration.class));
        }
    }
}
