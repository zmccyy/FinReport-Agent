package com.finreport.service;

import java.time.LocalDateTime;
import java.util.Comparator;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.http.HttpStatus;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.finreport.domain.dto.ChatDtos.ChatMessageResponse;
import com.finreport.domain.dto.ChatDtos.ChatSessionResponse;
import com.finreport.domain.dto.ChatDtos.ChatStreamRequest;
import com.finreport.domain.dto.ChatDtos.CreateSessionRequest;
import com.finreport.domain.dto.ChatDtos.SendMessageRequest;
import com.finreport.domain.entity.ChatMessage;
import com.finreport.domain.entity.ChatSession;
import com.finreport.domain.entity.Report;
import com.finreport.exception.BusinessException;
import com.finreport.mq.ChatMessageProducer;
import com.finreport.repository.ChatMessageRepository;
import com.finreport.repository.ChatSessionRepository;
import com.finreport.repository.ReportRepository;
import com.finreport.service.sse.ChatStreamProxy;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 问答服务 — spec §6.2.3 / M5.05。
 *
 * <p>职责：会话 CRUD + 消息收发。发消息链路（spec §3.3 混合模式）：
 * 校验归属 → 落库用户消息 → 装配上下文（最近 10 轮）→ 发布 MQ 控制流
 * 消息（尽力而为）→ WebClient 拉流 L3 SSE → 透传前端；token 事件累计
 * 答案文本，收到 done 事件后落库 assistant 消息并转发 done。</p>
 *
 * <p>事件数据面与控制面解耦：MQ 发布失败只记 WARN，不影响 SSE 流。</p>
 */
@Service
public class ChatService {

    private static final Logger log = LoggerFactory.getLogger(ChatService.class);

    private final ChatSessionRepository sessionRepository;
    private final ChatMessageRepository messageRepository;
    private final ReportRepository reportRepository;
    private final SessionContextService contextService;
    private final ChatStreamProxy streamProxy;
    private final ChatMessageProducer messageProducer;
    private final ObjectMapper objectMapper;

    public ChatService(
            ChatSessionRepository sessionRepository,
            ChatMessageRepository messageRepository,
            ReportRepository reportRepository,
            SessionContextService contextService,
            ChatStreamProxy streamProxy,
            ChatMessageProducer messageProducer) {
        this.sessionRepository = sessionRepository;
        this.messageRepository = messageRepository;
        this.reportRepository = reportRepository;
        this.contextService = contextService;
        this.streamProxy = streamProxy;
        this.messageProducer = messageProducer;
        this.objectMapper = new ObjectMapper();
    }

    // ========================================================================
    // 会话 CRUD
    // ========================================================================

    /**
     * 创建问答会话（校验报表归属当前用户）。
     *
     * @param userId  当前用户 ID
     * @param request 创建请求（reportId + 可选标题）
     * @return 创建的会话
     * @throws BusinessException 报表不存在或不归属当前用户
     */
    public Mono<ChatSessionResponse> createSession(Long userId, CreateSessionRequest request) {
        log.debug("[ChatService] createSession userId={} reportId={}", userId, request.reportId());
        return reportRepository.findById(request.reportId())
                .filter(report -> userId.equals(report.getUserId()))
                .switchIfEmpty(Mono.error(reportNotFound(request.reportId())))
                .flatMap(report -> sessionRepository.save(ChatSession.builder()
                        .userId(userId)
                        .reportId(report.getId())
                        .title(defaultTitle(request.title(), report))
                        .createdAt(LocalDateTime.now())
                        .updatedAt(LocalDateTime.now())
                        .build()))
                .map(session -> new ChatSessionResponse(
                        session.getId(), session.getReportId(), session.getTitle()));
    }

    /**
     * 查询当前用户的会话列表（更新时间倒序）。
     *
     * @param userId 当前用户 ID
     * @return 会话摘要列表
     */
    public Flux<ChatSessionResponse> listSessions(Long userId) {
        log.debug("[ChatService] listSessions userId={}", userId);
        return sessionRepository.findAllByUserIdOrderByUpdatedAtDesc(userId)
                .map(session -> new ChatSessionResponse(
                        session.getId(), session.getReportId(), session.getTitle()));
    }

    /**
     * 查询会话历史消息（时间正序）。
     *
     * @param sessionId 会话 ID
     * @param userId    当前用户 ID（归属校验）
     * @return 消息列表
     * @throws BusinessException 会话不存在或不属于当前用户
     */
    public Flux<ChatMessageResponse> listMessages(Long sessionId, Long userId) {
        log.debug("[ChatService] listMessages sessionId={} userId={}", sessionId, userId);
        return requireSession(sessionId, userId)
                .flatMapMany(ignored -> messageRepository
                        .findBySessionIdOrderByCreatedAtDesc(sessionId)
                        .collectList())
                .flatMap(messages -> Flux.fromIterable(messages.stream()
                        .sorted(Comparator.comparing(ChatMessage::getCreatedAt))
                        .map(this::toResponse)
                        .toList()));
    }

    /**
     * 删除会话及其全部消息。
     *
     * @param sessionId 会话 ID
     * @param userId    当前用户 ID（归属校验）
     * @return 完成信号
     */
    public Mono<Void> deleteSession(Long sessionId, Long userId) {
        log.debug("[ChatService] deleteSession sessionId={} userId={}", sessionId, userId);
        return requireSession(sessionId, userId)
                .flatMap(session -> messageRepository
                        .deleteBySessionId(sessionId)
                        .then(sessionRepository.deleteById(sessionId)));
    }

    // ========================================================================
    // 消息发送（SSE 流式）
    // ========================================================================

    /**
     * 发送消息并返回 L3 SSE 事件流（spec §6.3.3）。
     *
     * <p>事件透传规则：thought / tool_call / tool_result / token 原样转发
     * （token 内容同步累计为答案文本）；done 事件在落库 assistant 消息后
     * 转发；error 事件原样转发（L3 失败不落库）。</p>
     *
     * @param sessionId 会话 ID
     * @param userId    当前用户 ID（归属校验）
     * @param request   消息内容
     * @return 透传的 SSE 事件流（含终态 done / error）
     */
    public Flux<ServerSentEvent<String>> sendMessage(
            Long sessionId, Long userId, SendMessageRequest request) {
        log.debug("[ChatService] sendMessage sessionId={} userId={}", sessionId, userId);
        String content = request.content() == null ? "" : request.content().trim();
        if (content.isEmpty()) {
            return Flux.error(new BusinessException(
                    HttpStatus.BAD_REQUEST, "CHAT_CONTENT_EMPTY", "消息内容不能为空"));
        }
        return requireSession(sessionId, userId)
                .flatMapMany(session -> messageRepository
                        .save(ChatMessage.builder()
                                .sessionId(sessionId)
                                .role("user")
                                .content(content)
                                .createdAt(LocalDateTime.now())
                                .build())
                        // messageId = 用户消息的 DB 自增 ID（spec §6.3.3 done.messageId
                        // 即消息 ID，前端可据此关联已持久化的消息）。
                        .flatMapMany(saved -> streamExchange(
                                session, userId, content, String.valueOf(saved.getId()))));
    }

    /**
     * 装配 L3 请求并拉流：公司上下文 + 最近 10 轮历史 + 摘要 + MQ 控制流 + SSE 透传。
     */
    private Flux<ServerSentEvent<String>> streamExchange(
            ChatSession session, Long userId, String content, String messageId) {
        TokenAccumulator accumulator = new TokenAccumulator(objectMapper);
        return reportRepository.findById(session.getReportId())
                .flatMapMany(report -> contextService.loadContext(userId, session.getId())
                        .flatMapMany(context -> {
                            ChatStreamRequest l3Request = new ChatStreamRequest(
                                    String.valueOf(session.getId()),
                                    messageId,
                                    session.getReportId(),
                                    content,
                                    companyContext(report),
                                    context.turns(),
                                    context.summary());
                            // 控制流（尽力而为，失败不影响数据面）。
                            messageProducer.publishChat(l3Request, traceId());
                            log.info("[ChatService] 问答开始 sessionId={} messageId={} questionLength={}",
                                    session.getId(), messageId, content.length());
                            return streamProxy.stream(l3Request, traceId())
                                    .concatMap(event -> handleEvent(event, session, userId, content, accumulator));
                        }))
                .onErrorResume(error -> {
                    log.warn("[ChatService] 问答链路异常 sessionId={} messageId={} error={}",
                            session.getId(), messageId, error.getMessage());
                    return Flux.just(errorEvent(error));
                });
    }

    /**
     * 处理一条透传事件：token 累计、done 落库 + 会话上下文追加、其余原样转发。
     */
    private Mono<ServerSentEvent<String>> handleEvent(
            ServerSentEvent<String> event, ChatSession session, Long userId, String userContent,
            TokenAccumulator accumulator) {
        String eventName = event.event() == null ? "message" : event.event();
        switch (eventName) {
            case "token":
                accumulator.append(event.data());
                return Mono.just(event); // 原样透传前端
            case "done":
                return onDone(event, session, userId, userContent, accumulator);
            case "error":
                log.warn("[ChatService] L3 问答错误 sessionId={} data={}",
                        session.getId(), event.data());
                return Mono.just(event);
            default:
                return Mono.just(event);
        }
    }

    /**
     * done 事件：把累计的答案文本落库为 assistant 消息、异步追加会话上下文
     * （超过 10 轮触发摘要压缩，M5.06），然后转发 done 给前端。
     */
    private Mono<ServerSentEvent<String>> onDone(
            ServerSentEvent<String> event, ChatSession session, Long userId, String userContent,
            TokenAccumulator accumulator) {
        return Mono.fromCallable(() -> parseDone(event.data()))
                .flatMap(done -> {
                    ChatMessage assistant = ChatMessage.builder()
                            .sessionId(session.getId())
                            .role("assistant")
                            .content(accumulator.text())
                            .toolsUsed(serialize(done.toolsUsed()))
                            .tokenCount(done.tokenCount())
                            .createdAt(LocalDateTime.now())
                            .build();
                    return messageRepository.save(assistant)
                            // 上下文追加是后台尽力而为（压缩失败内部降级），不阻塞 SSE done。
                            .flatMap(saved -> {
                                contextService.appendRound(
                                        userId, session.getId(), userContent, saved.getContent())
                                        .subscribe(null, error -> log.warn(
                                                "[ChatService] 会话上下文追加失败 sessionId={} error={}",
                                                session.getId(), error.getMessage()));
                                return Mono.just(event);
                            });
                });
    }

    // ========================================================================
    // 私有工具
    // ========================================================================

    /**
     * 校验会话存在且归属当前用户。
     */
    private Mono<ChatSession> requireSession(Long sessionId, Long userId) {
        return sessionRepository.findByIdAndUserId(sessionId, userId)
                .switchIfEmpty(Mono.error(sessionNotFound(sessionId)));
    }

    private static String companyContext(Report report) {
        StringBuilder context = new StringBuilder();
        if (report.getCompanyName() != null && !report.getCompanyName().isBlank()) {
            context.append(report.getCompanyName());
        }
        if (report.getCompanyCode() != null && !report.getCompanyCode().isBlank()) {
            context.append('（').append(report.getCompanyCode()).append('）');
        }
        if (report.getReportPeriod() != null && !report.getReportPeriod().isBlank()) {
            context.append("，报告期 ").append(report.getReportPeriod());
        }
        return context.toString();
    }

    private static String defaultTitle(String requested, Report report) {
        if (requested != null && !requested.isBlank()) {
            return requested.trim();
        }
        return report.getCompanyName() == null ? "财报问答" : report.getCompanyName() + " 问答";
    }

    private ChatMessageResponse toResponse(ChatMessage message) {
        return new ChatMessageResponse(
                message.getId(),
                message.getRole(),
                message.getContent(),
                parseTools(message.getToolsUsed()),
                message.getCreatedAt() == null ? null : message.getCreatedAt().toString());
    }

    private List<String> parseTools(String json) {
        if (json == null || json.isBlank()) {
            return List.of();
        }
        try {
            return objectMapper.readValue(json, new TypeReference<List<String>>() {
            });
        } catch (JsonProcessingException error) {
            log.warn("[ChatService] tools_used 解析失败: {}", error.getMessage());
            return List.of();
        }
    }

    private String serialize(List<String> tools) {
        try {
            return objectMapper.writeValueAsString(tools);
        } catch (JsonProcessingException error) {
            log.warn("[ChatService] tools_used 序列化失败: {}", error.getMessage());
            return "[]";
        }
    }

    /**
     * 解析 done 事件元数据（messageId/tokenCount/toolsUsed）。答案文本不
     * 在 done 中——由 token 事件累计（TokenAccumulator）。
     */
    private DonePayload parseDone(String data) {
        try {
            Map<String, Object> map = objectMapper.readValue(
                    data, new TypeReference<Map<String, Object>>() {
                    });
            Object tools = map.getOrDefault("toolsUsed", List.of());
            List<String> toolsList = tools instanceof List<?> raw
                    ? raw.stream().map(String::valueOf).toList()
                    : List.of();
            int tokenCount = map.get("tokenCount") instanceof Number n ? n.intValue() : 0;
            return new DonePayload(toolsList, tokenCount);
        } catch (Exception error) {
            log.warn("[ChatService] done 事件解析失败: {}", error.getMessage());
            return new DonePayload(List.of(), 0);
        }
    }

    private ServerSentEvent<String> errorEvent(Throwable error) {
        try {
            String data = objectMapper.writeValueAsString(Map.of(
                    "code", "CHAT_FAILED",
                    "message", error.getMessage() == null
                            ? error.getClass().getSimpleName()
                            : error.getMessage()));
            return ServerSentEvent.<String>builder().event("error").data(data).build();
        } catch (JsonProcessingException jsonError) {
            return ServerSentEvent.<String>builder().event("error")
                    .data("{\"code\":\"CHAT_FAILED\",\"message\":\"internal error\"}").build();
        }
    }

    /**
     * done 事件元数据（answer 文本由 TokenAccumulator 提供）。
     */
    private record DonePayload(List<String> toolsUsed, int tokenCount) {
    }

    /**
     * 答案文本累计器：把 SSE token 事件的 {@code data.content} 顺序拼接。
     */
    private static final class TokenAccumulator {
        private final StringBuilder builder = new StringBuilder();
        private final ObjectMapper objectMapper;

        private TokenAccumulator(ObjectMapper objectMapper) {
            this.objectMapper = objectMapper;
        }

        private void append(String data) {
            if (data == null || data.isBlank()) {
                return;
            }
            try {
                Map<String, Object> map = objectMapper.readValue(
                        data, new TypeReference<Map<String, Object>>() {
                        });
                Object content = map.get("content");
                if (content instanceof String text) {
                    builder.append(text);
                }
            } catch (Exception error) {
                log.warn("[ChatService] token 事件解析失败: {}", error.getMessage());
            }
        }

        private String text() {
            return builder.toString();
        }
    }

    private static BusinessException sessionNotFound(Long sessionId) {
        return new BusinessException(
                HttpStatus.NOT_FOUND, "SESSION_NOT_FOUND", "会话不存在: " + sessionId);
    }

    private static BusinessException reportNotFound(Long reportId) {
        return new BusinessException(
                HttpStatus.NOT_FOUND, "REPORT_NOT_FOUND", "报表不存在: " + reportId);
    }

    private static String traceId() {
        return MDC.get("traceId");
    }
}
