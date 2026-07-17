package com.finreport.service.sse;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.when;

import java.time.Duration;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;
import org.springframework.data.redis.core.ReactiveListOperations;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.data.redis.core.ReactiveValueOperations;
import org.springframework.http.codec.ServerSentEvent;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/** Durable SSE cursor, snapshot, and idempotency tests. */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class RedisSseEventStoreTest {

    @Mock
    private ReactiveRedisTemplate<String, String> redis;
    @Mock
    private ReactiveValueOperations<String, String> values;
    @Mock
    private ReactiveListOperations<String, String> lists;

    private RedisSseEventStore store;

    @BeforeEach
    void setUp() {
        when(redis.opsForValue()).thenReturn(values);
        when(redis.opsForList()).thenReturn(lists);
        when(redis.expire(anyString(), any(Duration.class))).thenReturn(Mono.just(true));
        store = new RedisSseEventStore(redis);
    }

    @Test
    void shouldAssignMonotonicTaskScopedSequenceBeforeBroadcast() {
        when(values.setIfAbsent(anyString(), anyString(), any(Duration.class))).thenReturn(Mono.just(true));
        when(values.increment(anyString())).thenReturn(Mono.just(7L));
        when(lists.rightPush(anyString(), anyString())).thenReturn(Mono.just(1L));
        when(lists.trim(anyString(), anyLong(), anyLong())).thenReturn(Mono.just(true));
        when(values.set(anyString(), anyString(), any(Duration.class))).thenReturn(Mono.just(true));

        ServerSentEvent<String> event = store.append("task-7", ServerSentEvent.<String>builder()
                .event("progress").data("{\"step\":\"PARSE\"}").build()).block();

        assertEquals("task-7:7", event.id());
        assertEquals("progress", event.event());
    }

    @Test
    void shouldIgnoreDuplicateEventWithoutCreatingSequence() {
        when(values.setIfAbsent(anyString(), anyString(), any(Duration.class))).thenReturn(Mono.just(false));

        assertTrue(store.append("task-1", ServerSentEvent.<String>builder()
                        .event("progress").data("{}").build()).blockOptional().isEmpty());
    }

    @Test
    void shouldReplayOnlyEventsAfterValidCursorUsingPersistedTaskId() {
        when(lists.range(anyString(), anyLong(), anyLong())).thenReturn(Flux.just(
                "{\"taskId\":\"task-1\",\"sequence\":3,\"event\":\"progress\",\"data\":\"a\"}",
                "{\"taskId\":\"task-1\",\"sequence\":4,\"event\":\"done\",\"data\":\"b\"}"));

        StepVerifier.create(store.replay("task-1", "task-1:3"))
                .assertNext(event -> {
                    assertEquals("task-1:4", event.id());
                    assertEquals("done", event.event());
                })
                .verifyComplete();
    }

    @Test
    void shouldReturnSnapshotForForeignOrExpiredCursor() {
        String snapshot = "{\"taskId\":\"task-1\",\"sequence\":9,\"event\":\"progress\",\"data\":\"latest\"}";
        when(values.get(anyString())).thenReturn(Mono.just(snapshot));
        when(lists.range(anyString(), anyLong(), anyLong())).thenReturn(Flux.just(
                "{\"taskId\":\"task-1\",\"sequence\":5,\"event\":\"progress\",\"data\":\"old\"}"));

        StepVerifier.create(store.replay("task-1", "other:5"))
                .assertNext(event -> assertEquals("task-1:9", event.id()))
                .verifyComplete();
        StepVerifier.create(store.replay("task-1", "task-1:1"))
                .assertNext(event -> assertEquals("task-1:9", event.id()))
                .verifyComplete();
    }
}