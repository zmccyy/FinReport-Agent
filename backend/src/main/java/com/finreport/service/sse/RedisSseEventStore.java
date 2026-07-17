package com.finreport.service.sse;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.List;

import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * Redis-backed progress event history used for SSE reconnect recovery.
 *
 * <p>The in-process emitter pool is intentionally not the source of truth. Redis stores the
 * latest snapshot and the most recent 256 events for 24 hours, while a Redis atomic increment
 * supplies an event sequence that is monotonic per task.</p>
 */
@Service
public class RedisSseEventStore {

    private static final Duration TTL = Duration.ofHours(24);
    private static final long HISTORY_LIMIT = 256;
    private static final String PREFIX = "fin:task:progress:";

    private final ReactiveRedisTemplate<String, String> redisTemplate;
    private final ObjectMapper objectMapper;

    public RedisSseEventStore(ReactiveRedisTemplate<String, String> redisTemplate) {
        this.redisTemplate = redisTemplate;
        this.objectMapper = new ObjectMapper();
    }

    /**
     * Persist a progress event before it is broadcast to local SSE subscribers.
     * Duplicate step/status/data notifications are ignored for the retention window.
     *
     * @param taskId task identifier
     * @param event unsequenced SSE event
     * @return the sequenced event, or empty when it was an idempotent duplicate
     */
    public Mono<ServerSentEvent<String>> append(String taskId, ServerSentEvent<String> event) {
        String fingerprint = fingerprint(event.event() + "|" + event.data());
        String dedupeKey = prefix(taskId) + "dedupe:" + fingerprint;
        return redisTemplate.opsForValue().setIfAbsent(dedupeKey, "1", TTL)
                .flatMap(inserted -> {
                    if (!Boolean.TRUE.equals(inserted)) {
                        return Mono.empty();
                    }
                    return redisTemplate.opsForValue().increment(sequenceKey(taskId))
                            .flatMap(sequence -> persist(taskId, sequence, event));
                });
    }

    /**
     * Returns replay events after Last-Event-ID. Invalid or expired cursors receive a snapshot.
     *
     * @param taskId task identifier
     * @param lastEventId event id supplied by the client
     * @return missing history or, when history is unavailable, the latest snapshot
     */
    public Flux<ServerSentEvent<String>> replay(String taskId, String lastEventId) {
        Long cursor = parseCursor(taskId, lastEventId);
        if (lastEventId != null && cursor == null) {
            return latestSnapshot(taskId).flux();
        }
        if (cursor == null) {
            return Flux.empty();
        }
        return redisTemplate.opsForList().range(historyKey(taskId), 0, -1)
                .flatMap(this::decode)
                .collectList()
                .flatMapMany(events -> replayFromEvents(taskId, cursor, events));
    }

    /**
     * Returns the most recently persisted event for a task.
     *
     * @param taskId task identifier
     * @return latest event if retained
     */
    public Mono<ServerSentEvent<String>> latestSnapshot(String taskId) {
        return redisTemplate.opsForValue().get(snapshotKey(taskId)).flatMap(this::decode);
    }

    private Mono<ServerSentEvent<String>> persist(
            String taskId, long sequence, ServerSentEvent<String> event) {
        ServerSentEvent<String> sequenced = buildEvent(taskId + ":" + sequence, event.event(), event.data());
        PersistedEvent persisted = new PersistedEvent(taskId, sequence, sequenced.event(), sequenced.data());
        try {
            String encoded = objectMapper.writeValueAsString(persisted);
            return redisTemplate.opsForList().rightPush(historyKey(taskId), encoded)
                    .then(redisTemplate.opsForList().trim(historyKey(taskId), -HISTORY_LIMIT, -1))
                    .then(redisTemplate.opsForValue().set(snapshotKey(taskId), encoded, TTL))
                    .then(redisTemplate.expire(sequenceKey(taskId), TTL))
                    .then(redisTemplate.expire(historyKey(taskId), TTL))
                    .thenReturn(sequenced);
        } catch (Exception error) {
            return Mono.error(error);
        }
    }

    private Flux<ServerSentEvent<String>> replayFromEvents(
            String taskId, long cursor, List<ServerSentEvent<String>> events) {
        if (events.isEmpty()) {
            return latestSnapshot(taskId).flux();
        }
        long first = sequence(events.get(0));
        if (cursor < first - 1 || cursor > sequence(events.get(events.size() - 1))) {
            return latestSnapshot(taskId).flux();
        }
        return Flux.fromIterable(events).filter(event -> sequence(event) > cursor);
    }

    private Mono<ServerSentEvent<String>> decode(String value) {
        try {
            PersistedEvent persisted = objectMapper.readValue(value, new TypeReference<>() { });
            return Mono.just(buildEvent(persistedEventId(persisted), persisted.event(), persisted.data()));
        } catch (JsonProcessingException error) {
            return Mono.empty();
        }
    }

    private static ServerSentEvent<String> buildEvent(String id, String eventType, String data) {
        ServerSentEvent.Builder<String> builder = ServerSentEvent.<String>builder().id(id).data(data);
        if (eventType != null) {
            builder.event(eventType);
        }
        return builder.build();
    }

    private String persistedEventId(PersistedEvent persisted) {
        return persisted.taskId() + ":" + persisted.sequence();
    }

    private static long sequence(ServerSentEvent<String> event) {
        String id = event.id();
        int separator = id == null ? -1 : id.lastIndexOf(':');
        return separator < 0 ? -1 : Long.parseLong(id.substring(separator + 1));
    }

    private static Long parseCursor(String taskId, String lastEventId) {
        if (lastEventId == null || lastEventId.isBlank()) {
            return null;
        }
        String prefix = taskId + ":";
        if (!lastEventId.startsWith(prefix)) {
            return null;
        }
        try {
            long value = Long.parseLong(lastEventId.substring(prefix.length()));
            return value >= 0 ? value : null;
        } catch (NumberFormatException ignored) {
            return null;
        }
    }

    private static String fingerprint(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder result = new StringBuilder();
            for (byte current : digest) {
                result.append(String.format("%02x", current));
            }
            return result.toString();
        } catch (Exception error) {
            throw new IllegalStateException("Unable to hash SSE event", error);
        }
    }

    private static String prefix(String taskId) {
        return PREFIX + taskId + ":";
    }

    private static String sequenceKey(String taskId) {
        return prefix(taskId) + "seq";
    }

    private static String historyKey(String taskId) {
        return prefix(taskId) + "events";
    }

    private static String snapshotKey(String taskId) {
        return prefix(taskId) + "snapshot";
    }

    private record PersistedEvent(String taskId, long sequence, String event, String data) {
    }
}
