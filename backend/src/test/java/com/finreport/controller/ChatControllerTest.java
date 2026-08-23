package com.finreport.controller;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.codec.ServerSentEvent;

import com.finreport.domain.dto.ChatDtos.ChatSessionResponse;
import com.finreport.domain.dto.ChatDtos.CreateSessionRequest;
import com.finreport.domain.dto.ChatDtos.SendMessageRequest;
import com.finreport.service.ChatService;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/** ChatController 边界测试：路由与 X-User-Id 透传。 */
@ExtendWith(MockitoExtension.class)
class ChatControllerTest {

    @Mock
    private ChatService chatService;

    @Test
    void shouldDelegateCreateSessionWithUserId() {
        ChatController controller = new ChatController(chatService);
        when(chatService.createSession(7L, new CreateSessionRequest(17L, "标题")))
                .thenReturn(Mono.just(new ChatSessionResponse(1L, 17L, "标题")));

        StepVerifier.create(controller.createSession(7L, new CreateSessionRequest(17L, "标题")))
                .expectNextMatches(response -> response.getBody() != null
                        && response.getBody().id() == 1L)
                .verifyComplete();
    }

    @Test
    void shouldDelegateSendMessageAndReturnEventStream() {
        ChatController controller = new ChatController(chatService);
        when(chatService.sendMessage(eq(1L), eq(7L), any(SendMessageRequest.class)))
                .thenReturn(Flux.just(
                        ServerSentEvent.<String>builder().event("token").data("{\"content\":\"a\"}").build(),
                        ServerSentEvent.<String>builder().event("done").data("{}").build()));

        StepVerifier.create(controller.sendMessage(1L, 7L, new SendMessageRequest("问题")))
                .expectNextCount(2)
                .verifyComplete();

        verify(chatService).sendMessage(eq(1L), eq(7L), any(SendMessageRequest.class));
    }

    @Test
    void shouldDelegateDeleteSession() {
        ChatController controller = new ChatController(chatService);
        when(chatService.deleteSession(1L, 7L)).thenReturn(Mono.empty());

        StepVerifier.create(controller.deleteSession(1L, 7L))
                .expectNextMatches(response -> response.getStatusCode().is2xxSuccessful())
                .verifyComplete();
    }
}
