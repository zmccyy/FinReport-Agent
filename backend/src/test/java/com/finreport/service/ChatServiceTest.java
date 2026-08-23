package com.finreport.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.LocalDateTime;
import java.util.List;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.codec.ServerSentEvent;

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
import reactor.test.StepVerifier;

/** ChatService 单测：会话 CRUD + SSE 透传链路。 */
@ExtendWith(MockitoExtension.class)
class ChatServiceTest {

    @Mock
    private ChatSessionRepository sessionRepository;
    @Mock
    private ChatMessageRepository messageRepository;
    @Mock
    private ReportRepository reportRepository;
    @Mock
    private SessionContextService contextService;
    @Mock
    private ChatMessageProducer messageProducer;
    @Mock
    private ChatStreamProxy streamProxy;

    private ChatService service;

    @BeforeEach
    void setUp() {
        service = new ChatService(
                sessionRepository, messageRepository, reportRepository,
                contextService, streamProxy, messageProducer);
    }

    private static Report report(Long id, Long userId) {
        return Report.builder().id(id).userId(userId).companyName("贵州茅台")
                .companyCode("600519").reportPeriod("2025-12-31").build();
    }

    private static ChatSession session(Long id, Long userId, Long reportId) {
        return ChatSession.builder().id(id).userId(userId).reportId(reportId)
                .title("默认标题").createdAt(LocalDateTime.now()).updatedAt(LocalDateTime.now()).build();
    }

    // ----------------------------------------------------------------------
    // 会话 CRUD
    // ----------------------------------------------------------------------

    @Test
    void shouldCreateSessionWithDefaultTitleFromReport() {
        when(reportRepository.findById(17L)).thenReturn(Mono.just(report(17L, 7L)));
        when(sessionRepository.save(any(ChatSession.class)))
                .thenAnswer(invocation -> Mono.just(invocation.getArgument(0)));

        StepVerifier.create(service.createSession(7L, new CreateSessionRequest(17L, null)))
                .expectNextMatches(response -> response.reportId() == 17L
                        && "贵州茅台 问答".equals(response.title()))
                .verifyComplete();
    }

    @Test
    void shouldRejectCreateSessionForForeignReport() {
        when(reportRepository.findById(17L)).thenReturn(Mono.just(report(17L, 8L)));

        StepVerifier.create(service.createSession(7L, new CreateSessionRequest(17L, null)))
                .expectErrorMatches(error -> error instanceof BusinessException
                        && "REPORT_NOT_FOUND".equals(((BusinessException) error).getErrorCode()))
                .verify();
    }

    @Test
    void shouldListMessagesInChronologicalOrder() {
        when(sessionRepository.findByIdAndUserId(1L, 7L)).thenReturn(Mono.just(session(1L, 7L, 17L)));
        ChatMessage early = ChatMessage.builder().id(1L).sessionId(1L).role("user")
                .content("营收？").createdAt(LocalDateTime.of(2026, 8, 1, 10, 0)).build();
        ChatMessage late = ChatMessage.builder().id(2L).sessionId(1L).role("assistant")
                .content("1688 亿").toolsUsed("[\"query_statement\"]").tokenCount(2)
                .createdAt(LocalDateTime.of(2026, 8, 1, 10, 1)).build();
        when(messageRepository.findBySessionIdOrderByCreatedAtDesc(1L))
                .thenReturn(Flux.just(late, early));

        StepVerifier.create(service.listMessages(1L, 7L))
                .expectNextMatches(message -> "user".equals(message.role())
                        && "营收？".equals(message.content()))
                .expectNextMatches(message -> "assistant".equals(message.role())
                        && List.of("query_statement").equals(message.toolsUsed()))
                .verifyComplete();
    }

    @Test
    void shouldRejectMessagesOfForeignSession() {
        when(sessionRepository.findByIdAndUserId(1L, 7L)).thenReturn(Mono.empty());

        StepVerifier.create(service.listMessages(1L, 7L))
                .expectErrorMatches(error -> error instanceof BusinessException
                        && "SESSION_NOT_FOUND".equals(((BusinessException) error).getErrorCode()))
                .verify();
    }

    // ----------------------------------------------------------------------
    // 消息发送（SSE 透传）
    // ----------------------------------------------------------------------

    @Test
    void shouldStreamAndPersistAccumulatedAnswerOnDone() {
        ChatSession chat = session(1L, 7L, 17L);
        when(sessionRepository.findByIdAndUserId(1L, 7L)).thenReturn(Mono.just(chat));
        when(messageRepository.save(any(ChatMessage.class))).thenAnswer(i -> {
            ChatMessage saved = i.getArgument(0);
            return Mono.just(saved.getId() == null ? withId(saved) : saved);
        });
        when(reportRepository.findById(17L)).thenReturn(Mono.just(report(17L, 7L)));
        when(contextService.loadContext(7L, 1L)).thenReturn(Mono.just(new SessionContextService.SessionContext("", List.of())));
        when(contextService.appendRound(anyLong(), anyLong(), any(String.class), any(String.class)))
                .thenReturn(Mono.empty());
        when(streamProxy.stream(any(ChatStreamRequest.class), eq(null)))
                .thenReturn(Flux.just(
                        sse("token", "{\"content\":\"营收\"}"),
                        sse("token", "{\"content\":\" 1688 亿\"}"),
                        sse("done", "{\"messageId\":\"100\",\"tokenCount\":2,\"toolsUsed\":[\"query_statement\"],\"finishedReason\":\"final_answer\",\"error\":\"\"}")));

        StepVerifier.create(service.sendMessage(1L, 7L, new SendMessageRequest("营收？")))
                .expectNextCount(2) // token
                .expectNextMatches(event -> "done".equals(event.event()))
                .verifyComplete();

        // 用户消息 + assistant 消息共 2 次落库
        verify(messageRepository, org.mockito.Mockito.times(2)).save(any(ChatMessage.class));
        // assistant 消息内容 = token 累计
        verify(messageRepository).save(argThatSaved(assistant -> "营收 1688 亿".equals(assistant.getContent())
                && "[\"query_statement\"]".equals(assistant.getToolsUsed())
                && 2 == assistant.getTokenCount()));
    }

    @Test
    void shouldPassThroughErrorEventWithoutPersistingAssistant() {
        ChatSession chat = session(1L, 7L, 17L);
        when(sessionRepository.findByIdAndUserId(1L, 7L)).thenReturn(Mono.just(chat));
        when(messageRepository.save(any(ChatMessage.class))).thenAnswer(i -> {
            ChatMessage saved = i.getArgument(0);
            return Mono.just(saved.getId() == null ? withId(saved) : saved);
        });
        when(reportRepository.findById(17L)).thenReturn(Mono.just(report(17L, 7L)));
        when(contextService.loadContext(7L, 1L)).thenReturn(Mono.just(new SessionContextService.SessionContext("", List.of())));
        when(streamProxy.stream(any(ChatStreamRequest.class), eq(null)))
                .thenReturn(Flux.just(sse("error", "{\"code\":\"CHAT_FAILED\",\"message\":\"boom\"}")));

        StepVerifier.create(service.sendMessage(1L, 7L, new SendMessageRequest("问题")))
                .expectNextMatches(event -> "error".equals(event.event()))
                .verifyComplete();
    }

    @Test
    void shouldRejectEmptyContent() {
        StepVerifier.create(service.sendMessage(1L, 7L, new SendMessageRequest("  ")))
                .expectErrorMatches(error -> error instanceof BusinessException
                        && "CHAT_CONTENT_EMPTY".equals(((BusinessException) error).getErrorCode()))
                .verify();
    }

    @Test
    void shouldRejectSendToMissingSession() {
        when(sessionRepository.findByIdAndUserId(1L, 7L)).thenReturn(Mono.empty());

        StepVerifier.create(service.sendMessage(1L, 7L, new SendMessageRequest("问题")))
                .expectErrorMatches(error -> error instanceof BusinessException
                        && "SESSION_NOT_FOUND".equals(((BusinessException) error).getErrorCode()))
                .verify();
    }

    @Test
    void shouldPassReportContextAndHistoryToProxy() {
        ChatSession chat = session(1L, 7L, 17L);
        when(sessionRepository.findByIdAndUserId(1L, 7L)).thenReturn(Mono.just(chat));
        when(messageRepository.save(any(ChatMessage.class))).thenAnswer(i -> {
            ChatMessage saved = i.getArgument(0);
            return Mono.just(saved.getId() == null ? withId(saved) : saved);
        });
        when(reportRepository.findById(17L)).thenReturn(Mono.just(report(17L, 7L)));
        when(contextService.loadContext(7L, 1L)).thenReturn(Mono.just(new SessionContextService.SessionContext(
                "旧摘要", List.of(new com.finreport.domain.dto.ChatDtos.ChatTurn("user", "之前的问题")))));
        when(contextService.appendRound(anyLong(), anyLong(), any(String.class), any(String.class)))
                .thenReturn(Mono.empty());
        when(streamProxy.stream(any(ChatStreamRequest.class), eq(null)))
                .thenReturn(Flux.just(sse("done", "{\"messageId\":\"m\",\"tokenCount\":0,\"toolsUsed\":[],\"finishedReason\":\"final_answer\",\"error\":\"\"}")));

        StepVerifier.create(service.sendMessage(1L, 7L, new SendMessageRequest("追问")))
                .expectNextCount(1)
                .verifyComplete();

        verify(streamProxy).stream(org.mockito.ArgumentMatchers.argThat(request ->
                "100".equals(request.messageId())
                        && "旧摘要".equals(request.summary())
                        && request.history().size() == 1
                        && "贵州茅台（600519），报告期 2025-12-31".equals(request.companyContext())), eq(null));
        verify(messageProducer).publishChat(any(ChatStreamRequest.class), eq(null));
        // done 后异步追加会话上下文（fire-and-forget；本用例无 token 事件，答案为空）
        verify(contextService).appendRound(7L, 1L, "追问", "");
    }

    private static ChatMessage withId(ChatMessage message) {
        ChatMessage copy = ChatMessage.builder().id(100L).sessionId(message.getSessionId())
                .role(message.getRole()).content(message.getContent())
                .toolsUsed(message.getToolsUsed()).tokenCount(message.getTokenCount())
                .createdAt(message.getCreatedAt()).build();
        return copy;
    }

    private static ServerSentEvent<String> sse(String event, String data) {
        return ServerSentEvent.<String>builder().event(event).data(data).build();
    }

    private static ChatMessage argThatSaved(java.util.function.Predicate<ChatMessage> predicate) {
        return org.mockito.ArgumentMatchers.argThat(
                (org.mockito.ArgumentMatcher<ChatMessage>) predicate::test);
    }
}
