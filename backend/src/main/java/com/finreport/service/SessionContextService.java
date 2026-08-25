package com.finreport.service;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.reactive.function.client.WebClient;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.finreport.domain.dto.ChatDtos.ChatTurn;
import com.finreport.exception.IntegrationException;
import com.finreport.repository.ChatMessageRepository;

import reactor.core.publisher.Mono;

/**
 * 会话上下文管理 — spec §5.2「会话上下文 | Hash | 存最近 10 轮，超长触发压缩」/ M5.06。
 *
 * <p>Redis Hash {@code fin:session:{userId}:{sessionId}}（spec §5.4.1，TTL 24h）三个字段：</p>
 * <ul>
 *   <li>{@code history}：最近 10 轮（20 条消息）的 JSON 数组</li>
 *   <li>{@code summary}：窗口外轮次的 LLM 压缩摘要（关键事实保留）</li>
 *   <li>{@code rounds}：累计轮数（第 11 轮起触发压缩）</li>
 * </ul>
 *
 * <p>key 含 userId 维度（多租户隔离，spec §5.4.1）；读取走 Redis 优先、DB 回源
 * 回填（chat_message 表是权威存储）；每轮追加后若历史超过 10 轮，把「旧摘要 +
 * 被挤出窗口的消息」交给 L3 {@code /internal/chat/compress} 压缩成新摘要，
 * 历史裁剪回最近 10 轮。压缩失败降级为保留原始历史（下次追加再试），不丢上下文。</p>
 */
@Service
public class SessionContextService {

    private static final Logger log = LoggerFactory.getLogger(SessionContextService.class);

    /** 注入 L3 的上下文轮数上限（spec：最近 10 轮）。 */
    public static final int CONTEXT_ROUNDS = 10;

    /** 最近 10 轮 = 20 条消息（user + assistant）。 */
    public static final int CONTEXT_MESSAGES = CONTEXT_ROUNDS * 2;

    private static final Duration TTL = Duration.ofHours(24);
    private static final String KEY_PREFIX = "fin:session:";
    private static final String FIELD_HISTORY = "history";
    private static final String FIELD_SUMMARY = "summary";
    private static final String FIELD_ROUNDS = "rounds";

    private final ChatMessageRepository chatMessageRepository;
    private final ReactiveRedisTemplate<String, String> redisTemplate;
    private final WebClient webClient;
    private final ObjectMapper objectMapper;

    public SessionContextService(
            ChatMessageRepository chatMessageRepository,
            ReactiveRedisTemplate<String, String> redisTemplate,
            WebClient.Builder webClientBuilder,
            @Value("${ai-service.base-url}") String aiBaseUrl) {
        this.chatMessageRepository = chatMessageRepository;
        this.redisTemplate = redisTemplate;
        this.webClient = webClientBuilder.baseUrl(aiBaseUrl).build();
        this.objectMapper = new ObjectMapper();
    }

    /**
     * 会话上下文（摘要 + 最近 10 轮，供 L3 prompt 注入）。
     *
     * @param summary 压缩摘要（可能为空）
     * @param turns   时间正序的最近对话轮次
     */
    public record SessionContext(String summary, List<ChatTurn> turns) {
        /** 防御性拷贝：调用方无法改动返回的上下文（spotbugs EI_EXPOSE_REP）。 */
        public SessionContext {
            turns = turns == null ? List.of() : List.copyOf(turns);
        }
    }

    /**
     * 读取会话上下文：Redis 优先，miss 时回源 chat_message 表并回填。
     *
     * @param userId    用户 ID（Redis key 维度，spec §5.4.1 多租户隔离）
     * @param sessionId 会话 ID
     * @return 上下文（turns 时间正序，可能为空）
     */
    public Mono<SessionContext> loadContext(Long userId, Long sessionId) {
        log.debug("[SessionContextService] loadContext userId={} sessionId={}", userId, sessionId);
        String key = hashKey(userId, sessionId);
        return redisTemplate.<String, String>opsForHash()
                .multiGet(key, List.of(FIELD_SUMMARY, FIELD_HISTORY))
                .flatMap(values -> {
                    String summary = values.isEmpty() || values.get(0) == null ? "" : values.get(0);
                    String historyJson = values.size() < 2 ? null : values.get(1);
                    if (historyJson != null && !historyJson.isBlank()) {
                        return Mono.just(new SessionContext(summary, decodeTurns(historyJson)));
                    }
                    return loadFromDbAndBackfill(key, sessionId);
                });
    }

    /**
     * 追加一轮问答并维护窗口：超过 10 轮时触发压缩（尽力而为，失败降级）。
     *
     * <p>压缩在返回的 Mono 内完成；调用方按 fire-and-forget 订阅即可，
     * 失败已被降级吸收，不会把错误传播到 SSE 链路。</p>
     *
     * @param userId          用户 ID（Redis key 维度，spec §5.4.1）
     * @param sessionId       会话 ID
     * @param userMessage     用户消息
     * @param assistantMessage 助手回答
     * @return 完成信号（压缩失败也正常完成）
     */
    public Mono<Void> appendRound(
            Long userId, Long sessionId, String userMessage, String assistantMessage) {
        log.debug("[SessionContextService] appendRound userId={} sessionId={}", userId, sessionId);
        String key = hashKey(userId, sessionId);
        return redisTemplate.<String, String>opsForHash()
                .multiGet(key, List.of(FIELD_SUMMARY, FIELD_HISTORY, FIELD_ROUNDS))
                .flatMap(values -> {
                    String summary = values.isEmpty() || values.get(0) == null ? "" : values.get(0);
                    String historyJson = values.size() < 2 || values.get(1) == null ? "[]" : values.get(1);
                    long rounds = values.size() < 3 || values.get(2) == null ? 0L
                            : Long.parseLong(String.valueOf(values.get(2)));
                    List<ChatTurn> history = decodeTurns(historyJson);
                    history.add(new ChatTurn("user", userMessage));
                    history.add(new ChatTurn("assistant", assistantMessage));
                    long newRounds = rounds + 1;

                    if (history.size() <= CONTEXT_MESSAGES) {
                        return persist(key, summary, history, newRounds).then();
                    }
                    // 第 11 轮起：旧摘要 + 被挤出窗口的消息 → 新摘要，历史裁剪回 10 轮。
                    List<ChatTurn> overflow = new ArrayList<>(history.subList(0, history.size() - CONTEXT_MESSAGES));
                    List<ChatTurn> window = new ArrayList<>(history.subList(history.size() - CONTEXT_MESSAGES, history.size()));
                    return compress(userId, sessionId, summary, overflow)
                            .flatMap(newSummary -> persist(key, newSummary, window, newRounds))
                            .onErrorResume(error -> {
                                // 压缩失败降级：保留全部历史（下次追加再试），不丢上下文。
                                log.warn("[SessionContextService] 压缩失败 sessionId={} error={}，保留原始历史",
                                        sessionId, error.getMessage());
                                return persist(key, summary, history, newRounds);
                            })
                            .then();
                });
    }

    /**
     * 调用 L3 压缩会话历史（仅内部缓存更新，权威存储是 chat_message 表）。
     *
     * @param userId    用户 ID（压缩请求透传）
     * @param sessionId 会话 ID（压缩请求透传）
     * @param summary   已有摘要
     * @param overflow  被挤出窗口的消息
     * @return 新摘要文本
     */
    private Mono<String> compress(Long userId, Long sessionId, String summary, List<ChatTurn> overflow) {
        List<Map<String, String>> messages = new ArrayList<>();
        if (!summary.isBlank()) {
            messages.add(Map.of("role", "assistant", "content", "此前摘要：" + summary));
        }
        for (ChatTurn turn : overflow) {
            messages.add(Map.of("role", turn.role(), "content", turn.content()));
        }
        return webClient.post()
                .uri("/internal/chat/compress")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(Map.of("sessionId", String.valueOf(sessionId), "messages", messages))
                .retrieve()
                .bodyToMono(Map.class)
                .mapNotNull(body -> body == null ? null : String.valueOf(body.get("summary")))
                .filter(newSummary -> !newSummary.isBlank())
                .switchIfEmpty(Mono.error(new IntegrationException(
                        HttpStatus.BAD_GATEWAY,
                        "AI_SERVICE_COMPRESS_FAILED",
                        "L3 返回空摘要，会话上下文压缩失败")));
    }

    private Mono<Void> persist(String key, String summary, List<ChatTurn> history, long rounds) {
        Map<String, String> fields = Map.of(
                FIELD_SUMMARY, summary == null ? "" : summary,
                FIELD_HISTORY, encodeTurns(history),
                FIELD_ROUNDS, String.valueOf(rounds));
        return redisTemplate.opsForHash().putAll(key, fields)
                .then(redisTemplate.expire(key, TTL).then());
    }

    /**
     * Redis miss 时回源 chat_message 表（权威存储）并回填缓存。
     */
    private Mono<SessionContext> loadFromDbAndBackfill(String key, Long sessionId) {
        return chatMessageRepository.findBySessionIdOrderByCreatedAtDesc(sessionId)
                .take(CONTEXT_MESSAGES)
                .map(message -> new ChatTurn(message.getRole(), message.getContent()))
                .collectList()
                .map(SessionContextService::reverseOrder)
                .flatMap(turns -> redisTemplate.opsForHash().putAll(key, Map.of(
                        FIELD_HISTORY, encodeTurns(turns),
                        FIELD_ROUNDS, String.valueOf(0)))
                        .then(redisTemplate.expire(key, TTL))
                        .thenReturn(new SessionContext("", turns)));
    }

    private String encodeTurns(List<ChatTurn> turns) {
        try {
            return objectMapper.writeValueAsString(turns);
        } catch (Exception error) {
            log.warn("[SessionContextService] 历史序列化失败: {}", error.getMessage());
            return "[]";
        }
    }

    private List<ChatTurn> decodeTurns(String json) {
        try {
            return objectMapper.readValue(json, new TypeReference<List<ChatTurn>>() {
            });
        } catch (Exception error) {
            log.warn("[SessionContextService] 历史解析失败: {}", error.getMessage());
            return new ArrayList<>();
        }
    }

    private static String hashKey(Long userId, Long sessionId) {
        return KEY_PREFIX + userId + ":" + sessionId;
    }

    private static List<ChatTurn> reverseOrder(List<ChatTurn> turns) {
        List<ChatTurn> ordered = new ArrayList<>(turns);
        Collections.reverse(ordered);
        return ordered;
    }
}
