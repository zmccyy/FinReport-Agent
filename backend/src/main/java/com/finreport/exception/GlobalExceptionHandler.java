package com.finreport.exception;

import java.time.Instant;
import java.util.Map;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.server.ServerWebExchange;

import com.finreport.trace.TraceContext;

import reactor.core.publisher.Mono;

/**
 * 全局异常处理 — RFC 9457 Problem Details。
 *
 * <p>统一拦截所有 Controller 抛出的异常，返回标准化的错误响应。
 * 错误码体系参见 CLAUDE.md §10。</p>
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    private static final String ERROR_TYPE_PREFIX = "https://finreport.example/errors/";

    /**
     * 处理业务异常。
     */
    @ExceptionHandler(BusinessException.class)
    public Mono<ResponseEntity<Map<String, Object>>> handleBusinessException(
            BusinessException ex, ServerWebExchange exchange) {
        log.warn("[BusinessException] code={} status={} message={}", ex.getErrorCode(), ex.getStatus(), ex.getMessage());
        return buildErrorResponse(exchange, ex.getStatus(), ex.getErrorCode(), ex.getMessage());
    }

    /**
     * 处理集成异常。
     */
    @ExceptionHandler(IntegrationException.class)
    public Mono<ResponseEntity<Map<String, Object>>> handleIntegrationException(
            IntegrationException ex, ServerWebExchange exchange) {
        log.error("[IntegrationException] code={} status={} message={}", ex.getErrorCode(), ex.getStatus(), ex.getMessage(), ex);
        return buildErrorResponse(exchange, ex.getStatus(), ex.getErrorCode(), ex.getMessage());
    }

    /**
     * 处理 Spring Validation 异常。
     */
    @ExceptionHandler(org.springframework.web.bind.support.WebExchangeBindException.class)
    public Mono<ResponseEntity<Map<String, Object>>> handleValidation(
            org.springframework.web.bind.support.WebExchangeBindException ex, ServerWebExchange exchange) {
        String detail = ex.getFieldErrors().stream()
                .map(e -> e.getField() + ": " + e.getDefaultMessage())
                .reduce((a, b) -> a + "; " + b)
                .orElse("validation failed");
        log.debug("[Validation] {}", detail);
        return buildErrorResponse(exchange, HttpStatus.UNPROCESSABLE_ENTITY, "VALIDATION_ERROR", detail);
    }

    /**
     * 兜底处理所有未捕获异常。
     */
    @ExceptionHandler(Exception.class)
    public Mono<ResponseEntity<Map<String, Object>>> handleGeneral(
            Exception ex, ServerWebExchange exchange) {
        log.error("[Unhandled] {}: {}", ex.getClass().getName(), ex.getMessage(), ex);
        return buildErrorResponse(exchange, HttpStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "服务器内部错误");
    }

    // --- helpers ---

    private Mono<ResponseEntity<Map<String, Object>>> buildErrorResponse(
            ServerWebExchange exchange, HttpStatus status, String errorCode, String detail) {
        String traceId = exchange.getAttribute(TraceContext.TRACE_ID);
        if (traceId == null || traceId.isBlank()) {
            traceId = UUID.randomUUID().toString();
        }

        Map<String, Object> body = Map.of(
                "type", ERROR_TYPE_PREFIX + errorCode,
                "title", status.getReasonPhrase(),
                "status", status.value(),
                "detail", detail,
                "instance", exchange.getRequest().getPath().value(),
                "traceId", traceId,
                "timestamp", Instant.now().toString()
        );
        return Mono.just(ResponseEntity.status(status).body(body));
    }
}
