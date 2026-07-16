package com.finreport.trace;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;

import java.util.concurrent.atomic.AtomicReference;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.mock.http.server.reactive.MockServerHttpRequest;
import org.springframework.mock.web.server.MockServerWebExchange;

import reactor.core.publisher.Mono;

/**
 * {@link TraceIdWebFilter} 单元测试。
 */
@DisplayName("TraceIdWebFilter")
class TraceIdWebFilterTest {

    @Test
    @DisplayName("should preserve request trace ID in response and Reactor Context")
    void shouldPreserveRequestTraceIdInResponseAndReactorContext() {
        TraceIdWebFilter filter = new TraceIdWebFilter();
        MockServerWebExchange exchange = MockServerWebExchange.from(MockServerHttpRequest.get("/api/v1/reports")
                .header(TraceContext.TRACE_ID_HEADER, "trace-from-client")
                .build());
        AtomicReference<String> contextTraceId = new AtomicReference<>();
        AtomicReference<String> exchangeTraceId = new AtomicReference<>();

        filter.filter(exchange, tracedExchange -> Mono.deferContextual(context -> {
            contextTraceId.set(context.get(TraceContext.TRACE_ID));
            exchangeTraceId.set(tracedExchange.getAttribute(TraceContext.TRACE_ID));
            return Mono.empty();
        })).block();

        assertEquals("trace-from-client", contextTraceId.get());
        assertEquals("trace-from-client", exchangeTraceId.get());
        assertEquals("trace-from-client",
                exchange.getResponse().getHeaders().getFirst(TraceContext.TRACE_ID_HEADER));
    }

    @Test
    @DisplayName("should create trace ID when request does not provide one")
    void shouldCreateTraceIdWhenRequestDoesNotProvideOne() {
        TraceIdWebFilter filter = new TraceIdWebFilter();
        MockServerWebExchange exchange = MockServerWebExchange.from(MockServerHttpRequest.get("/api/v1/reports").build());
        AtomicReference<String> contextTraceId = new AtomicReference<>();

        filter.filter(exchange, tracedExchange -> Mono.deferContextual(context -> {
            contextTraceId.set(context.get(TraceContext.TRACE_ID));
            return Mono.empty();
        })).block();

        assertNotNull(contextTraceId.get());
        assertFalse(contextTraceId.get().isBlank());
        assertEquals(contextTraceId.get(),
                exchange.getResponse().getHeaders().getFirst(TraceContext.TRACE_ID_HEADER));
    }
}
