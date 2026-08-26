package com.finreport.controller;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.finreport.domain.dto.ChatDtos.ChatMessageResponse;
import com.finreport.domain.dto.ChatDtos.ChatSessionResponse;
import com.finreport.domain.dto.ChatDtos.CreateSessionRequest;
import com.finreport.domain.dto.ChatDtos.SendMessageRequest;
import com.finreport.idempotent.Idempotent;
import com.finreport.ratelimit.RateLimit;
import com.finreport.service.ChatService;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 问答控制器 — spec §6.2.3 / M5.05。
 *
 * <p>会话 CRUD + 消息发送（SSE 流式响应）。用户身份取自 JWT 过滤器注入的
 * {@code X-User-Id} 头，所有操作按用户隔离（spec §8.5）。</p>
 */
@RestController
@RequestMapping("/api/v1/chat")
public class ChatController {

    private static final Logger log = LoggerFactory.getLogger(ChatController.class);

    private final ChatService chatService;

    public ChatController(ChatService chatService) {
        this.chatService = chatService;
    }

    /**
     * 创建问答会话。
     *
     * @param userId  当前用户 ID（JWT 过滤器注入）
     * @param request 创建请求（reportId + 可选标题）
     * @return 创建的会话
     */
    @PostMapping("/sessions")
    @RateLimit(name = "chat-session-create", limit = 10, windowSeconds = 60)
    @Idempotent(name = "chat-session-create")
    public Mono<ResponseEntity<ChatSessionResponse>> createSession(
            @RequestHeader("X-User-Id") Long userId,
            @RequestBody CreateSessionRequest request) {
        log.debug("[ChatController] POST /chat/sessions userId={}", userId);
        return chatService.createSession(userId, request)
                .map(ResponseEntity::ok);
    }

    /**
     * 查询当前用户的会话列表。
     *
     * @param userId 当前用户 ID
     * @return 会话摘要列表（更新时间倒序）
     */
    @GetMapping("/sessions")
    public Flux<ChatSessionResponse> listSessions(
            @RequestHeader("X-User-Id") Long userId) {
        log.debug("[ChatController] GET /chat/sessions userId={}", userId);
        return chatService.listSessions(userId);
    }

    /**
     * 查询会话历史消息（时间正序）。
     *
     * @param sessionId 会话 ID
     * @param userId    当前用户 ID
     * @return 消息列表
     */
    @GetMapping("/sessions/{id}/messages")
    public Flux<ChatMessageResponse> listMessages(
            @PathVariable("id") Long sessionId,
            @RequestHeader("X-User-Id") Long userId) {
        log.debug("[ChatController] GET /chat/sessions/{}/messages userId={}", sessionId, userId);
        return chatService.listMessages(sessionId, userId);
    }

    /**
     * 发送消息，SSE 流式返回问答事件（spec §6.3.3）。
     *
     * <p>事件流：thought → tool_call → tool_result → token… → done（或 error）。
     * 收到 done 后服务端关闭连接。</p>
     *
     * @param sessionId 会话 ID
     * @param userId    当前用户 ID
     * @param request   消息内容
     * @return text/event-stream 事件流
     */
    @PostMapping(value = "/sessions/{id}/messages", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    @RateLimit(name = "chat-send", limit = 10, windowSeconds = 60)
    public Flux<ServerSentEvent<String>> sendMessage(
            @PathVariable("id") Long sessionId,
            @RequestHeader("X-User-Id") Long userId,
            @RequestBody SendMessageRequest request) {
        log.debug("[ChatController] POST /chat/sessions/{}/messages userId={}", sessionId, userId);
        return chatService.sendMessage(sessionId, userId, request);
    }

    /**
     * 删除会话及其全部消息。
     *
     * @param sessionId 会话 ID
     * @param userId    当前用户 ID
     * @return 204 No Content
     */
    @DeleteMapping("/sessions/{id}")
    public Mono<ResponseEntity<Void>> deleteSession(
            @PathVariable("id") Long sessionId,
            @RequestHeader("X-User-Id") Long userId) {
        log.debug("[ChatController] DELETE /chat/sessions/{} userId={}", sessionId, userId);
        return chatService.deleteSession(sessionId, userId)
                .thenReturn(ResponseEntity.noContent().build());
    }
}
