package com.finreport.service.sse;

import java.time.Duration;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

import com.finreport.domain.dto.ChatDtos.ChatStreamRequest;

import reactor.core.publisher.Flux;

/**
 * 问答 SSE 拉流代理 — spec §3.3 数据面（M5.05）。
 *
 * <p>以 WebClient POST 拉取 L3 {@code /internal/chat/stream} 的
 * {@code text/event-stream}，把 thought / tool_call / tool_result / token /
 * done / error 事件原样透传回 L2 调用方（ChatService 负责持久化与转发）。</p>
 */
@Component
public class ChatStreamProxy {

    private static final Logger log = LoggerFactory.getLogger(ChatStreamProxy.class);

    /** 整流超时。spec §3.7 单轮 60s 超时以单次生成为口径；多步 ReAct
     *  单轮实测可达 100s+（见 L3 chat.py CHAT_STREAM_TIMEOUT_SECONDS），
     *  150s 覆盖 L3 120s 整流上限后留网络余量，防前端 SSE 无限挂起。 */
    private static final Duration STREAM_TIMEOUT = Duration.ofSeconds(150);

    private static final ParameterizedTypeReference<ServerSentEvent<String>> SSE_TYPE =
            new ParameterizedTypeReference<>() {
            };

    private final WebClient webClient;

    /**
     * Creates the proxy against the configured L3 base URL.
     *
     * @param builder  Spring 提供的 WebClient 构建器
     * @param aiBaseUrl L3 base URL（application.yml ai-service.base-url）
     */
    public ChatStreamProxy(WebClient.Builder builder,
            @Value("${ai-service.base-url}") String aiBaseUrl) {
        this.webClient = builder.baseUrl(aiBaseUrl).build();
    }

    /**
     * 拉取一轮问答的 SSE 事件流。
     *
     * @param request L3 流式请求体
     * @param traceId 链路 trace ID（透传 L3 日志）
     * @return L3 SSE 事件流（含 done / error 终态）
     */
    public Flux<ServerSentEvent<String>> stream(ChatStreamRequest request, String traceId) {
        Map<String, Object> body = new java.util.HashMap<>();
        body.put("sessionId", request.sessionId());
        body.put("messageId", request.messageId());
        body.put("reportId", request.reportId());
        body.put("question", request.question());
        body.put("companyContext", request.companyContext() == null ? "" : request.companyContext());
        body.put("history", request.history());
        if (request.summary() != null && !request.summary().isBlank()) {
            body.put("summary", request.summary());
        }
        return webClient.post()
                .uri("/internal/chat/stream")
                .header(HttpHeaders.CONTENT_TYPE, MediaType.APPLICATION_JSON_VALUE)
                .header("X-Trace-Id", traceId)
                .bodyValue(body)
                .retrieve()
                .bodyToFlux(SSE_TYPE)
                .doOnError(error -> log.warn("[ChatStreamProxy] L3 拉流失败 messageId={} error={}",
                        request.messageId(), error.getMessage()))
                .timeout(STREAM_TIMEOUT);
    }
}
