package com.finreport.service.orchestrator;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Duration;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.data.redis.core.ReactiveValueOperations;

import com.finreport.domain.enums.TaskStepName;

import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * ExtractCompletionTracker 单元测试 — M2.08。
 *
 * <p>用 Mockito mock {@link ReactiveRedisTemplate} + {@link ReactiveValueOperations}，
 * 不需要真实 Redis。覆盖：</p>
 * <ul>
 *   <li>recordSuccess INCR + 首次 EXPIRE 路径</li>
 *   <li>recordFailure SET flag + TTL</li>
 *   <li>clearFailure / reset DEL 路径</li>
 *   <li>getCount / hasFailed 查询路径</li>
 *   <li>Redis 故障 fallback 返回默认值（不抛异常）</li>
 * </ul>
 */
@ExtendWith(MockitoExtension.class)
@DisplayName("ExtractCompletionTracker")
class ExtractCompletionTrackerTest {

    @Mock
    private ReactiveRedisTemplate<String, String> redisTemplate;

    @Mock
    private ReactiveValueOperations<String, String> valueOps;

    private ExtractCompletionTracker tracker;

    @BeforeEach
    void setUp() {
        lenient().when(redisTemplate.opsForValue()).thenReturn(valueOps);
        tracker = new ExtractCompletionTracker(redisTemplate);
    }

    @Nested
    @DisplayName("recordSuccess")
    class RecordSuccess {

        @Test
        @DisplayName("should INCR and set TTL only on first increment")
        void shouldIncrAndSetTtlOnlyOnFirstIncrement() {
            String taskId = "task-first";
            String key = ExtractCompletionTracker.KEY_DONE_PREFIX + taskId;
            when(valueOps.increment(key)).thenReturn(Mono.just(1L));
            when(redisTemplate.expire(eq(key), eq(ExtractCompletionTracker.KEY_TTL)))
                    .thenReturn(Mono.just(true));

            StepVerifier.create(tracker.recordSuccess(taskId, TaskStepName.EXTRACT_BS))
                    .expectNext(1)
                    .verifyComplete();

            verify(redisTemplate).expire(key, ExtractCompletionTracker.KEY_TTL);
        }

        @Test
        @DisplayName("should skip EXPIRE on subsequent increments")
        void shouldSkipExpireOnSubsequentIncrements() {
            String taskId = "task-second";
            String key = ExtractCompletionTracker.KEY_DONE_PREFIX + taskId;
            when(valueOps.increment(key)).thenReturn(Mono.just(2L));

            StepVerifier.create(tracker.recordSuccess(taskId, TaskStepName.EXTRACT_IS))
                    .expectNext(2)
                    .verifyComplete();

            verify(redisTemplate, never()).expire(anyString(), any(Duration.class));
        }

        @Test
        @DisplayName("should reach EXPECTED_COUNT on third success")
        void shouldReachExpectedCountOnThirdSuccess() {
            String taskId = "task-third";
            String key = ExtractCompletionTracker.KEY_DONE_PREFIX + taskId;
            when(valueOps.increment(key)).thenReturn(Mono.just(3L));

            StepVerifier.create(tracker.recordSuccess(taskId, TaskStepName.EXTRACT_CF))
                    .expectNext(3)
                    .verifyComplete();

            assertEquals(3, ExtractCompletionTracker.EXPECTED_COUNT);
        }

        @Test
        @DisplayName("should return 0 when Redis fails (fallback to MySQL)")
        void shouldReturnZeroWhenRedisFails() {
            String taskId = "task-redis-down";
            String key = ExtractCompletionTracker.KEY_DONE_PREFIX + taskId;
            when(valueOps.increment(key)).thenReturn(Mono.error(new RuntimeException("connection refused")));

            StepVerifier.create(tracker.recordSuccess(taskId, TaskStepName.EXTRACT_BS))
                    .expectNext(0)
                    .verifyComplete();
        }
    }

    @Nested
    @DisplayName("recordFailure")
    class RecordFailure {

        @Test
        @DisplayName("should SET failed flag with TTL")
        void shouldSetFailedFlagWithTtl() {
            String taskId = "task-failed";
            String key = ExtractCompletionTracker.KEY_FAILED_PREFIX + taskId;
            when(valueOps.set(eq(key), eq(TaskStepName.EXTRACT_BS.name()),
                    eq(ExtractCompletionTracker.KEY_TTL)))
                    .thenReturn(Mono.just(true));

            StepVerifier.create(tracker.recordFailure(taskId, TaskStepName.EXTRACT_BS))
                    .verifyComplete();

            verify(valueOps).set(key, TaskStepName.EXTRACT_BS.name(),
                    ExtractCompletionTracker.KEY_TTL);
        }

        @Test
        @DisplayName("should swallow Redis errors silently")
        void shouldSwallowRedisErrorsSilently() {
            String taskId = "task-failed-redis-down";
            String key = ExtractCompletionTracker.KEY_FAILED_PREFIX + taskId;
            when(valueOps.set(eq(key), anyString(), eq(ExtractCompletionTracker.KEY_TTL)))
                    .thenReturn(Mono.error(new RuntimeException("timeout")));

            StepVerifier.create(tracker.recordFailure(taskId, TaskStepName.EXTRACT_IS))
                    .verifyComplete();
        }
    }

    @Nested
    @DisplayName("clearFailure / reset")
    class ClearAndReset {

        @Test
        @DisplayName("clearFailure should DEL only the failed key")
        void clearFailureShouldDelOnlyFailedKey() {
            String taskId = "task-clear";
            String failedKey = ExtractCompletionTracker.KEY_FAILED_PREFIX + taskId;
            when(redisTemplate.delete(failedKey)).thenReturn(Mono.just(1L));

            StepVerifier.create(tracker.clearFailure(taskId))
                    .verifyComplete();

            verify(redisTemplate).delete(failedKey);
        }

        @Test
        @DisplayName("reset should DEL both done and failed keys")
        void resetShouldDelBothKeys() {
            String taskId = "task-reset";
            String doneKey = ExtractCompletionTracker.KEY_DONE_PREFIX + taskId;
            String failedKey = ExtractCompletionTracker.KEY_FAILED_PREFIX + taskId;
            when(redisTemplate.delete(doneKey, failedKey)).thenReturn(Mono.just(2L));

            StepVerifier.create(tracker.reset(taskId))
                    .verifyComplete();

            verify(redisTemplate).delete(doneKey, failedKey);
        }

        @Test
        @DisplayName("reset should swallow Redis errors silently")
        void resetShouldSwallowRedisErrorsSilently() {
            String taskId = "task-reset-down";
            when(redisTemplate.delete(anyString(), anyString()))
                    .thenReturn(Mono.error(new RuntimeException("connection refused")));

            StepVerifier.create(tracker.reset(taskId))
                    .verifyComplete();
        }
    }

    @Nested
    @DisplayName("getCount / hasFailed")
    class QueryOps {

        @Test
        void getCountShouldReturnParsedValue() {
            String taskId = "task-count";
            String key = ExtractCompletionTracker.KEY_DONE_PREFIX + taskId;
            when(valueOps.get(key)).thenReturn(Mono.just("2"));

            StepVerifier.create(tracker.getCount(taskId))
                    .expectNext(2)
                    .verifyComplete();
        }

        @Test
        void getCountShouldDefaultToZeroWhenKeyMissing() {
            String taskId = "task-empty";
            String key = ExtractCompletionTracker.KEY_DONE_PREFIX + taskId;
            when(valueOps.get(key)).thenReturn(Mono.empty());

            StepVerifier.create(tracker.getCount(taskId))
                    .expectNext(0)
                    .verifyComplete();
        }

        @Test
        void getCountShouldReturnZeroOnRedisError() {
            String taskId = "task-count-down";
            String key = ExtractCompletionTracker.KEY_DONE_PREFIX + taskId;
            when(valueOps.get(key)).thenReturn(Mono.error(new RuntimeException("down")));

            StepVerifier.create(tracker.getCount(taskId))
                    .expectNext(0)
                    .verifyComplete();
        }

        @Test
        void hasFailedShouldReturnTrueWhenKeyExists() {
            String taskId = "task-has-failed";
            String key = ExtractCompletionTracker.KEY_FAILED_PREFIX + taskId;
            when(redisTemplate.hasKey(key)).thenReturn(Mono.just(true));

            StepVerifier.create(tracker.hasFailed(taskId))
                    .expectNext(true)
                    .verifyComplete();
        }

        @Test
        void hasFailedShouldReturnFalseOnRedisError() {
            String taskId = "task-has-failed-down";
            String key = ExtractCompletionTracker.KEY_FAILED_PREFIX + taskId;
            when(redisTemplate.hasKey(key)).thenReturn(Mono.error(new RuntimeException("down")));

            StepVerifier.create(tracker.hasFailed(taskId))
                    .expectNext(false)
                    .verifyComplete();
        }
    }
}
