package com.finreport.trace;

import java.util.UUID;

import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.server.reactive.ServerHttpRequest;
import org.springframework.stereotype.Component;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.WebFilter;
import org.springframework.web.server.WebFilterChain;

import reactor.core.publisher.Mono;

/**
 * 在 HTTP、ServerWebExchange 与 Reactor Context 之间传递 trace ID。
 */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class TraceIdWebFilter implements WebFilter {

    /**
     * 解析或生成 trace ID，并将其写入响应、交换属性和 Reactor Context。
     *
     * @param exchange 当前 HTTP 交换
     * @param chain 后续过滤器链
     * @return 带 trace ID 上下文的处理链
     */
    @Override
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        String traceId = resolveTraceId(exchange.getRequest());
        exchange.getResponse().getHeaders().set(TraceContext.TRACE_ID_HEADER, traceId);

        // M6.07：业务 traceId 写入当前 OTel span attribute——Jaeger UI 可按
        // finreport.traceId 标签检索，与日志（JSON logging 的 traceId 字段）
        // 对齐。无 javaagent 时 Span.current() 为 INVALID_SPAN，setAttribute no-op。
        io.opentelemetry.api.trace.Span.current().setAttribute("finreport.traceId", traceId);

        ServerWebExchange tracedExchange = exchange.mutate()
                .request(exchange.getRequest().mutate()
                        .header(TraceContext.TRACE_ID_HEADER, traceId)
                        .build())
                .build();
        tracedExchange.getAttributes().put(TraceContext.TRACE_ID, traceId);

        return chain.filter(tracedExchange)
                .contextWrite(context -> context.put(TraceContext.TRACE_ID, traceId));
    }

    private String resolveTraceId(ServerHttpRequest request) {
        String suppliedTraceId = request.getHeaders().getFirst(TraceContext.TRACE_ID_HEADER);
        return suppliedTraceId == null || suppliedTraceId.isBlank()
                ? UUID.randomUUID().toString()
                : suppliedTraceId;
    }
}
