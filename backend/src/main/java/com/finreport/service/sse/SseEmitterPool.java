package com.finreport.service.sse;

import java.util.concurrent.ConcurrentHashMap;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Component;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Sinks;

/**
 * SSE 连接池 — spec §3.2 M3。
 *
 * <p>为每个任务使用独立的发射锁，确保来自 HTTP 与 MQ 线程的事件按顺序串行写入
 * {@link Sinks.Many}。这避免 {@code FAIL_NON_SERIALIZED} 时静默丢弃任务进度事件。</p>
 */
@Component
public class SseEmitterPool {

    private static final Logger log = LoggerFactory.getLogger(SseEmitterPool.class);

    /** 最多保留的历史事件数（用于断线重连）。 */
    private static final int REPLAY_HISTORY = 16;

    /** taskId → 任务专属 sink 与发射锁。 */
    private final ConcurrentHashMap<String, TaskSink> sinks = new ConcurrentHashMap<>();

    /**
     * 订阅某个任务的 SSE 事件流。
     *
     * @param taskId 任务 ID
     * @return SSE 事件流（含历史重放 + 实时推送）
     */
    public Flux<ServerSentEvent<String>> subscribe(String taskId) {
        TaskSink taskSink = sinks.computeIfAbsent(taskId, this::createTaskSink);
        return taskSink.replaySink().asFlux()
                .doOnCancel(() -> log.debug("[SseEmitterPool] 客户端取消订阅 taskId={} subCount={}",
                        taskId, taskSink.replaySink().currentSubscriberCount()))
                .doOnTerminate(() -> log.debug("[SseEmitterPool] Flux 终止 taskId={}", taskId));
    }

    /**
     * 向某任务的所有 SSE 订阅者推送事件。
     *
     * @param taskId 任务 ID
     * @param event SSE 事件
     * @return true 如果推送成功
     */
    public boolean emit(String taskId, ServerSentEvent<String> event) {
        // MQ progress can arrive before the client establishes its SSE subscription.
        // Creating the replay sink here preserves those events for the later subscriber.
        TaskSink taskSink = sinks.computeIfAbsent(taskId, this::createTaskSink);
        synchronized (taskSink.monitor()) {
            Sinks.EmitResult replayResult = taskSink.replaySink().tryEmitNext(event);
            Sinks.EmitResult result = taskSink.realtimeSink().tryEmitNext(event);
            if (replayResult.isFailure() && replayResult != Sinks.EmitResult.FAIL_ZERO_SUBSCRIBER) {
                log.warn("[SseEmitterPool] replay emit failed taskId={} result={}", taskId, replayResult);
            }
            if (result.isFailure() && result != Sinks.EmitResult.FAIL_ZERO_SUBSCRIBER) {
                log.warn("[SseEmitterPool] tryEmitNext 失败 taskId={} result={}", taskId, result);
                return false;
            }
            return true;
        }
    }

    /**
     * 关闭某个任务的 sink（任务终态时调用）。
     *
     * <p>已完成的 sink 保留在 map 中，以便断线重连的客户端接收历史终端事件后完成。</p>
     *
     * @param taskId 任务 ID
     */
    public void complete(String taskId) {
        TaskSink taskSink = sinks.get(taskId);
        if (taskSink == null) {
            return;
        }
        synchronized (taskSink.monitor()) {
            Sinks.EmitResult replayResult = taskSink.replaySink().tryEmitComplete();
            Sinks.EmitResult result = taskSink.realtimeSink().tryEmitComplete();
            if (replayResult.isFailure() && replayResult != Sinks.EmitResult.FAIL_TERMINATED) {
                log.warn("[SseEmitterPool] replay complete failed taskId={} result={}", taskId, replayResult);
            }
            if (result.isFailure() && result != Sinks.EmitResult.FAIL_TERMINATED) {
                log.warn("[SseEmitterPool] tryEmitComplete 失败 taskId={} result={}", taskId, result);
            }
        }
        log.debug("[SseEmitterPool] sink 已关闭（保留在 map 中） taskId={}", taskId);
    }

    private TaskSink createTaskSink(String taskId) {
        log.debug("[SseEmitterPool] 创建 sink taskId={}", taskId);
        return new TaskSink(
                Sinks.many().replay().limit(REPLAY_HISTORY),
                Sinks.many().multicast().directBestEffort(),
                new Object());
    }

    /**
     * Subscribe to real-time events only. Redis owns reconnect replay semantics.
     *
     * @param taskId task identifier
     * @return live event stream without in-memory history
     */
    public Flux<ServerSentEvent<String>> subscribeRealtime(String taskId) {
        TaskSink taskSink = sinks.computeIfAbsent(taskId, this::createTaskSink);
        return taskSink.realtimeSink().asFlux();
    }

    private record TaskSink(
            Sinks.Many<ServerSentEvent<String>> replaySink,
            Sinks.Many<ServerSentEvent<String>> realtimeSink,
            Object monitor) {
    }
}
