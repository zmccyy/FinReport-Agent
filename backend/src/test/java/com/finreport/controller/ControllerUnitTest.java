package com.finreport.controller;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Instant;
import java.util.Map;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.HttpStatus;
import org.springframework.http.codec.multipart.FilePart;

import com.finreport.domain.dto.LoginRequest;
import com.finreport.domain.dto.RefreshRequest;
import com.finreport.domain.dto.RegisterRequest;
import com.finreport.domain.dto.TokenResponse;
import com.finreport.domain.dto.UploadResponse;
import com.finreport.domain.dto.UserInfoResponse;
import com.finreport.domain.entity.Task;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.exception.AuthException;
import com.finreport.exception.BusinessException;
import com.finreport.service.AuthService;
import com.finreport.service.file.FileService;
import com.finreport.service.orchestrator.TaskOrchestrator;
import com.finreport.service.sse.SseEmitterPool;

import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/** Controller boundary and task ownership tests. */
@ExtendWith(MockitoExtension.class)
class ControllerUnitTest {

    @Mock
    private AuthService authService;
    @Mock
    private FileService fileService;
    @Mock
    private com.finreport.service.statement.StatementQueryService statementQueryService;
    @Mock
    private TaskOrchestrator orchestrator;
    @Mock
    private FilePart filePart;

    @Test
    void shouldDelegateAllAuthenticationOperations() {
        AuthController controller = new AuthController(authService);
        TokenResponse token = new TokenResponse("access", "refresh", 3600);
        RegisterRequest register = new RegisterRequest("alice", "secret1", "a@example.com");
        LoginRequest login = new LoginRequest("alice", "secret1");
        RefreshRequest refresh = new RefreshRequest("refresh");
        UserInfoResponse user = new UserInfoResponse(7L, "alice", "a@example.com", "USER", Instant.now());
        when(authService.register(register)).thenReturn(Mono.just(token));
        when(authService.login(login)).thenReturn(Mono.just(token));
        when(authService.refresh(refresh)).thenReturn(Mono.just(token));
        when(authService.logout("refresh")).thenReturn(Mono.empty());
        when(authService.getUserInfo(7L)).thenReturn(Mono.just(user));

        assertEquals(HttpStatus.CREATED, controller.register(register).block().getStatusCode());
        assertEquals(HttpStatus.OK, controller.login(login).block().getStatusCode());
        assertEquals(HttpStatus.OK, controller.refresh(refresh).block().getStatusCode());
        assertEquals(HttpStatus.NO_CONTENT, controller.logout(Map.of("refreshToken", "refresh")).block().getStatusCode());
        assertEquals(user, controller.getCurrentUser("7").block().getBody());
        verify(authService).getUserInfo(7L);
    }

    @Test
    void shouldRejectMissingOrMalformedAuthenticatedUser() {
        AuthController controller = new AuthController(authService);
        StepVerifier.create(controller.getCurrentUser(null))
                .expectError(AuthException.class)
                .verify();
        StepVerifier.create(controller.getCurrentUser("bad"))
                .expectError(AuthException.class)
                .verify();
    }

    @Test
    void shouldUploadWithCreatedResponseAndMapUnexpectedFailure() {
        ReportController controller = new ReportController(fileService);
        UploadResponse response = new UploadResponse("task-1", 11L, "PARSE_RUNNING");
        when(fileService.upload(eq(filePart), eq("600519"), eq("贵州茅台"), eq("ANNUAL"),
                eq("2025"), eq(7L), eq("idem-1"))).thenReturn(Mono.just(response));

        var created = controller.upload(Mono.just(filePart), "600519", "贵州茅台", "ANNUAL", "2025", 7L, "idem-1")
                .block();
        assertEquals(HttpStatus.CREATED, created.getStatusCode());
        assertEquals(response, created.getBody());

        when(fileService.upload(any(), any(), any(), any(), any(), any(), any()))
                .thenReturn(Mono.error(new IllegalStateException("minio unavailable")));
        StepVerifier.create(controller.upload(Mono.just(filePart), "1", "n", "ANNUAL", "2025", 7L, null))
                .expectErrorMatches(error -> error instanceof BusinessException business
                        && "UPLOAD_FAILED".equals(business.getErrorCode()))
                .verify();
    }

    @Test
    void shouldReturnTaskForOwnerAndHideForeignTask() {
        TaskController controller = new TaskController(orchestrator, new SseEmitterPool());
        Task task = task("task-1", TaskStatus.PARSE_RUNNING);
        when(orchestrator.findByIdAndUserId("task-1", 7L)).thenReturn(Mono.just(task));
        when(orchestrator.findByIdAndUserId("task-1", 8L)).thenReturn(Mono.empty());

        assertEquals(task, controller.getTask("task-1", 7L).block().getBody());
        StepVerifier.create(controller.getTask("task-1", 8L))
                .expectErrorMatches(error -> error instanceof BusinessException business
                        && "TASK_NOT_FOUND".equals(business.getErrorCode()))
                .verify();
    }

    @Test
    void shouldHideForeignStreamAndEmitTerminalFallbackForOwner() {
        TaskController controller = new TaskController(orchestrator, new SseEmitterPool());
        Task task = task("task-terminal", TaskStatus.COMPLETED);
        task.setRefReportId(99L);
        when(orchestrator.findByIdAndUserId("task-terminal", 7L)).thenReturn(Mono.just(task));
        when(orchestrator.findByIdAndUserId("task-terminal", 8L)).thenReturn(Mono.empty());

        StepVerifier.create(controller.streamProgress("task-terminal", 7L, null))
                .assertNext(event -> {
                    assertEquals("done", event.event());
                    assertEquals("task-terminal:terminal", event.id());
                })
                .verifyComplete();
        StepVerifier.create(controller.streamProgress("task-terminal", 8L, null))
                .expectErrorMatches(error -> error instanceof BusinessException business
                        && "TASK_NOT_FOUND".equals(business.getErrorCode()))
                .verify();
    }

    @Test
    void shouldCancelOnlyOwnedTask() {
        TaskController controller = new TaskController(orchestrator, new SseEmitterPool());
        Task cancelled = task("task-cancel", TaskStatus.CANCELLED);
        when(orchestrator.cancelTask("task-cancel", 7L)).thenReturn(Mono.just(cancelled));
        when(orchestrator.cancelTask("task-cancel", 8L)).thenReturn(Mono.error(
                new BusinessException(HttpStatus.NOT_FOUND, "TASK_NOT_FOUND", "任务不存在")));

        assertEquals(HttpStatus.OK, controller.cancelTask("task-cancel", 7L).block().getStatusCode());
        StepVerifier.create(controller.cancelTask("task-cancel", 8L))
                .expectErrorMatches(error -> error instanceof BusinessException business
                        && "TASK_NOT_FOUND".equals(business.getErrorCode()))
                .verify();
    }

    private static Task task(String id, TaskStatus status) {
        Task task = new Task();
        task.setId(id);
        task.setStatus(status.name());
        task.setUserId(7L);
        return task;
    }
}