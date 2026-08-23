package com.finreport.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.data.redis.core.ReactiveHashOperations;
import org.springframework.data.redis.core.ReactiveRedisTemplate;

import com.finreport.domain.dto.ChatDtos.ChatTurn;
import com.finreport.domain.entity.ChatMessage;
import com.finreport.repository.ChatMessageRepository;
import com.sun.net.httpserver.HttpServer;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * SessionContextService 单测：Redis 缓存回源、10 轮窗口、第 11 轮压缩、降级。
 *
 * <p>Redis 与 L3 compress 均为 mock/内嵌 HTTP 服务：本测试验证纯逻辑，
 * 不依赖真实基础设施。压缩请求走 JDK 内嵌 HttpServer 返回固定摘要。</p>
 */
@ExtendWith(MockitoExtension.class)
class SessionContextServiceTest {

    @Mock
    private ChatMessageRepository chatMessageRepository;
    @Mock
    private ReactiveRedisTemplate<String, String> redisTemplate;
    @Mock
    private ReactiveHashOperations<String, String, String> hashOps;

    private HttpServer compressServer;
    private int compressHits;
    private SessionContextService service;

    @BeforeEach
    void setUp() throws IOException {
        compressHits = 0;
        compressServer = HttpServer.create(new InetSocketAddress(0), 0);
        compressServer.createContext("/internal/chat/compress", exchange -> {
            compressHits++;
            byte[] response = "{\"summary\":\"压缩后的关键事实\"}".getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, response.length);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(response);
            }
        });
        compressServer.start();
        when(redisTemplate.<String, String>opsForHash()).thenReturn(hashOps);
        service = new SessionContextService(
                chatMessageRepository,
                redisTemplate,
                org.springframework.web.reactive.function.client.WebClient.builder(),
                "http://127.0.0.1:" + compressServer.getAddress().getPort());
    }

    @AfterEach
    void tearDown() {
        compressServer.stop(0);
    }

    // ----------------------------------------------------------------------
    // loadContext
    // ----------------------------------------------------------------------

    @Test
    void shouldBackfillFromDbWhenRedisMiss() {
        when(hashOps.multiGet(any(String.class), any(List.class)))
                .thenReturn(Mono.just(java.util.Arrays.asList("", null)));
        when(chatMessageRepository.findBySessionIdOrderByCreatedAtDesc(1L))
                .thenReturn(Flux.just(
                        message("assistant", "答"),
                        message("user", "问")));
        when(hashOps.putAll(any(String.class), any(java.util.Map.class))).thenReturn(Mono.just(true));
        when(redisTemplate.expire(any(String.class), any(java.time.Duration.class)))
                .thenReturn(Mono.just(true));

        StepVerifier.create(service.loadContext(1L))
                .assertNext(context -> {
                    assertEquals("", context.summary());
                    assertEquals(2, context.turns().size());
                    assertEquals("user", context.turns().get(0).role()); // 逆序为正序
                })
                .verifyComplete();

        verify(hashOps).putAll(any(String.class), any(java.util.Map.class)); // 回填
    }

    @Test
    void shouldReturnCachedContextWhenRedisHit() {
        String historyJson = "[{\"role\":\"user\",\"content\":\"问\"},{\"role\":\"assistant\",\"content\":\"答\"}]";
        when(hashOps.multiGet(any(String.class), any(List.class)))
                .thenReturn(Mono.just(List.of("旧摘要", historyJson)));

        StepVerifier.create(service.loadContext(1L))
                .assertNext(context -> {
                    assertEquals("旧摘要", context.summary());
                    assertEquals(2, context.turns().size());
                })
                .verifyComplete();

        verify(chatMessageRepository, never()).findBySessionIdOrderByCreatedAtDesc(anyLong());
    }

    // ----------------------------------------------------------------------
    // appendRound 与压缩
    // ----------------------------------------------------------------------

    @Test
    void shouldNotCompressWithinTenRounds() {
        // 9 轮 = 18 条历史 + 追加 1 轮 → 20 条 = 窗口上限，不触发压缩
        List<ChatTurn> nineRounds = tenRoundHistory().subList(0, 18);
        when(hashOps.multiGet(any(String.class), any(List.class)))
                .thenReturn(Mono.just(List.of("", encode(nineRounds), "9")));
        when(hashOps.putAll(any(String.class), any(java.util.Map.class))).thenReturn(Mono.just(true));
        when(redisTemplate.expire(any(String.class), any(java.time.Duration.class)))
                .thenReturn(Mono.just(true));

        StepVerifier.create(service.appendRound(1L, "第10轮问题", "第10轮回答"))
                .verifyComplete();

        assertEquals(0, compressHits, "10 轮内不应触发压缩");
    }

    @Test
    void shouldCompressOnEleventhRound() {
        // 10 轮 = 20 条历史 + 追加 1 轮 → 22 条 > 20，触发压缩
        List<ChatTurn> tenRounds = tenRoundHistory();
        when(hashOps.multiGet(any(String.class), any(List.class)))
                .thenReturn(Mono.just(List.of("", encode(tenRounds), "10")));
        when(hashOps.putAll(any(String.class), any(java.util.Map.class))).thenReturn(Mono.just(true));
        when(redisTemplate.expire(any(String.class), any(java.time.Duration.class)))
                .thenReturn(Mono.just(true));

        StepVerifier.create(service.appendRound(1L, "第11轮问题", "第11轮回答"))
                .verifyComplete();

        // 第 11 轮 → 压缩被调用（HTTP 服务器命中 1 次）
        assertEquals(1, compressHits, "第 11 轮应触发压缩");
    }

    @Test
    void shouldKeepRecentWindowAfterCompression() {
        when(hashOps.multiGet(any(String.class), any(List.class)))
                .thenReturn(Mono.just(List.of("旧摘要", encode(tenRoundHistory()), "10")));
        java.util.Map<String, String> captured = new java.util.concurrent.ConcurrentHashMap<>();
        when(hashOps.putAll(any(String.class), any(java.util.Map.class)))
                .thenAnswer(invocation -> {
                    @SuppressWarnings("unchecked")
                    java.util.Map<String, String> fields = invocation.getArgument(1);
                    captured.putAll(fields);
                    return Mono.just(true);
                });
        when(redisTemplate.expire(any(String.class), any(java.time.Duration.class)))
                .thenReturn(Mono.just(true));

        StepVerifier.create(service.appendRound(1L, "第11轮问题", "第11轮回答"))
                .verifyComplete();

        // 摘要被更新为 L3 压缩结果；历史裁剪回 20 条
        assertEquals("压缩后的关键事实", captured.get("summary"));
        List<ChatTurn> window = decode(captured.get("history"));
        assertEquals(20, window.size());
    }

    @Test
    void shouldKeepFullHistoryWhenCompressionFails() throws IOException {
        // 停止 compress 服务 → 调用失败 → 降级保留全部历史（22 条）不裁剪
        compressServer.stop(0);
        when(hashOps.multiGet(any(String.class), any(List.class)))
                .thenReturn(Mono.just(List.of("", encode(tenRoundHistory()), "10")));
        java.util.Map<String, String> captured = new java.util.concurrent.ConcurrentHashMap<>();
        when(hashOps.putAll(any(String.class), any(java.util.Map.class)))
                .thenAnswer(invocation -> {
                    @SuppressWarnings("unchecked")
                    java.util.Map<String, String> fields = invocation.getArgument(1);
                    captured.putAll(fields);
                    return Mono.just(true);
                });
        when(redisTemplate.expire(any(String.class), any(java.time.Duration.class)))
                .thenReturn(Mono.just(true));

        StepVerifier.create(service.appendRound(1L, "问题", "回答"))
                .verifyComplete(); // 不抛错

        assertEquals("", captured.get("summary")); // 摘要保持旧值
        List<ChatTurn> history = decode(captured.get("history"));
        assertEquals(22, history.size(), "压缩失败应保留全部历史（不丢上下文）");
    }

    // ----------------------------------------------------------------------
    // 工具
    // ----------------------------------------------------------------------

    private static ChatMessage message(String role, String content) {
        return ChatMessage.builder().sessionId(1L).role(role).content(content).build();
    }

    private static List<ChatTurn> tenRoundHistory() {
        List<ChatTurn> turns = new java.util.ArrayList<>();
        for (int i = 1; i <= 10; i++) {
            turns.add(new ChatTurn("user", "问题" + i));
            turns.add(new ChatTurn("assistant", "回答" + i));
        }
        return turns;
    }

    private static List<ChatTurn> decode(String json) {
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().readValue(
                    json, new com.fasterxml.jackson.core.type.TypeReference<List<ChatTurn>>() {
                    });
        } catch (Exception error) {
            throw new IllegalStateException(error);
        }
    }

    private static String encode(List<ChatTurn> turns) {
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().writeValueAsString(turns);
        } catch (Exception error) {
            throw new IllegalStateException(error);
        }
    }
}
