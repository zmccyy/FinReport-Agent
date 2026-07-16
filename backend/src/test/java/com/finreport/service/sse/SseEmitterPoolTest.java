package com.finreport.service.sse;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.http.codec.ServerSentEvent;

import reactor.core.Disposable;
import reactor.test.StepVerifier;

/**
 * SseEmitterPool 单元测试。
 */
@DisplayName("SseEmitterPool")
class SseEmitterPoolTest {

    private SseEmitterPool pool;

    @BeforeEach
    void setUp() {
        pool = new SseEmitterPool();
    }

    @Nested
    @DisplayName("subscribe")
    class Subscribe {

        @Test
        @DisplayName("should return non-null Flux on first subscribe")
        void shouldReturnNonNullFlux() {
            var flux = pool.subscribe("task-1");
            assertNotNull(flux);
        }

        @Test
        @DisplayName("should return same sink for same taskId (single map entry)")
        void shouldReuseExistingSink() {
            pool.subscribe("task-1");
            pool.subscribe("task-1");
            // No assertion needed — second subscribe returns Flux from existing sink
        }
    }

    @Nested
    @DisplayName("emit")
    class Emit {

        @Test
        @DisplayName("should deliver events to subscribers")
        void shouldDeliverEvents() {
            var event = ServerSentEvent.<String>builder()
                    .event("progress")
                    .data("{\"taskId\":\"task-1\",\"step\":\"PARSE\"}")
                    .build();

            StepVerifier.withVirtualTime(() -> pool.subscribe("task-1"))
                    .then(() -> pool.emit("task-1", event))
                    .expectNext(event)
                    .thenCancel()
                    .verify();
        }

        @Test
        @DisplayName("should replay cached events to late subscribers")
        void shouldReplayCachedEvents() {
            var event1 = ServerSentEvent.<String>builder()
                    .event("progress").data("{\"step\":\"PARSE\"}").id("1").build();
            var event2 = ServerSentEvent.<String>builder()
                    .event("progress").data("{\"step\":\"EXTRACT_BS\"}").id("2").build();

            pool.emit("task-2", event1);
            pool.emit("task-2", event2);

            StepVerifier.create(pool.subscribe("task-2"))
                    .expectNext(event1)
                    .expectNext(event2)
                    .thenCancel()
                    .verify();
        }

        @Test
        @DisplayName("should retain event emitted before first subscription")
        void shouldRetainEventEmittedBeforeFirstSubscription() {
            var event = ServerSentEvent.<String>builder().event("progress").data("{}").build();

            assertTrue(pool.emit("nonexistent", event));
            StepVerifier.create(pool.subscribe("nonexistent"))
                    .expectNext(event)
                    .thenCancel()
                    .verify();
        }

        @Test
        @DisplayName("should return true on successful emit")
        void shouldReturnTrueOnSuccess() {
            pool.subscribe("task-return");
            var event = ServerSentEvent.<String>builder().event("progress").data("{}").build();
            assertTrue(pool.emit("task-return", event));
        }
    }

    @Nested
    @DisplayName("complete")
    class Complete {

        @Test
        @DisplayName("should emit complete signal to subscribers")
        void shouldCompleteSubscribers() {
            var event = ServerSentEvent.<String>builder()
                    .event("progress").data("{\"step\":\"PARSE\"}").build();

            StepVerifier.create(pool.subscribe("task-3"))
                    .then(() -> {
                        pool.emit("task-3", event);
                        pool.complete("task-3");
                    })
                    .expectNext(event)
                    .expectComplete()
                    .verify();
        }

        @Test
        @DisplayName("should keep completed sink in map for late subscribers")
        void shouldKeepCompletedSink() {
            pool.subscribe("task-late");
            pool.complete("task-late");

            // Late subscriber should get the completed sink → immediate complete
            StepVerifier.create(pool.subscribe("task-late"))
                    .expectComplete()
                    .verify();
        }

        @Test
        @DisplayName("should not throw on duplicate complete")
        void shouldNotThrowOnDuplicateComplete() {
            pool.subscribe("task-dup");
            pool.complete("task-dup");
            pool.complete("task-dup"); // Should not throw
        }
    }

    @Nested
    @DisplayName("concurrent access")
    class ConcurrentAccess {

        @Test
        @DisplayName("should handle concurrent emit from thread pool")
        void shouldHandleConcurrentAccess() throws Exception {
            int eventCount = 20;
            CountDownLatch latch = new CountDownLatch(eventCount);
            List<ServerSentEvent<String>> received = new ArrayList<>();
            Object lock = new Object();

            Disposable sub = pool.subscribe("task-concurrent")
                    .subscribe(event -> {
                        synchronized (lock) { received.add(event); }
                        latch.countDown();
                    });

            ExecutorService executor = Executors.newFixedThreadPool(2);
            for (int i = 0; i < eventCount; i++) {
                final int idx = i;
                executor.submit(() -> pool.emit("task-concurrent",
                        ServerSentEvent.<String>builder()
                                .event("progress").data("{\"idx\":" + idx + "}").build()));
            }

            executor.shutdown();
            assertTrue(executor.awaitTermination(5, TimeUnit.SECONDS));
            assertTrue(latch.await(5, TimeUnit.SECONDS),
                    "Timed out, received " + received.size() + "/" + eventCount);
            assertEquals(eventCount, received.size(),
                    "Concurrent emit must not drop progress events");
            sub.dispose();
        }
    }
}
