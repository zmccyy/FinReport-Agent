package com.finreport.trace;

/**
 * 请求链路追踪上下文键。
 */
public final class TraceContext {

    /** HTTP 请求头、ServerWebExchange 属性和 Reactor Context 使用的 trace ID 键。 */
    public static final String TRACE_ID = "traceId";

    /** HTTP trace ID 请求和响应头名称。 */
    public static final String TRACE_ID_HEADER = "X-Trace-Id";

    private TraceContext() {
    }
}
