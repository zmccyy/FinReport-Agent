package com.finreport.metrics;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;

/**
 * L2 业务指标 — M6.05 / spec §7.2.3。
 *
 * <p>集中定义业务侧 Micrometer 指标，供任务编排、SSE、MQ 等组件埋点；
 * 系统侧指标（JVM/HTTP/Redis 连接池）由 actuator + micrometer 自动提供。</p>
 */
@Component
public class BusinessMetrics {

    private static final Logger log = LoggerFactory.getLogger(BusinessMetrics.class);

    /** 任务步骤终态计数（step × outcome=success/failed）。 */
    private final Counter stepSuccessTotal;

    /** 任务级终态计数（outcome=completed/failed）。 */
    private final Counter taskCompletedTotal;
    private final Counter taskFailedTotal;

    /** 步骤端到端耗时（含 MQ 往返 + L3 处理 + 落库）。 */
    private final Timer stepLatency;

    /** SSE 活跃连接数。 */
    private final io.micrometer.core.instrument.Gauge sseActiveConnections;

    /** MQ 任务消息发布计数。 */
    private final Counter mqPublishedTotal;

    private volatile int sseActive = 0;

    public BusinessMetrics(MeterRegistry registry) {
        this.stepSuccessTotal = Counter.builder("fin_step_total")
                .description("任务步骤终态总数")
                .tag("outcome", "success")
                .register(registry);
        this.taskCompletedTotal = Counter.builder("fin_task_total")
                .description("任务终态总数")
                .tag("outcome", "completed")
                .register(registry);
        this.taskFailedTotal = Counter.builder("fin_task_total")
                .description("任务终态总数")
                .tag("outcome", "failed")
                .register(registry);
        this.stepLatency = Timer.builder("fin_step_latency")
                .description("任务步骤端到端耗时（发布到终态）")
                .publishPercentiles(0.95)
                .register(registry);
        this.sseActiveConnections = io.micrometer.core.instrument.Gauge
                .builder("fin_sse_active_connections", this, self -> self.sseActive)
                .description("SSE 活跃连接数")
                .register(registry);
        this.mqPublishedTotal = Counter.builder("fin_mq_published_total")
                .description("MQ 任务消息发布总数")
                .register(registry);
        log.debug("[BusinessMetrics] 业务指标已注册");
    }

    /**
     * 记录一次步骤成功终态。
     *
     * @param stepName 步骤名（PARSE/EXTRACT_BS/.../REPORT）
     * @param latencyMillis 步骤端到端耗时（毫秒）
     */
    public void recordStepSuccess(String stepName, long latencyMillis) {
        stepSuccessTotal.increment();
        stepLatency.record(java.time.Duration.ofMillis(latencyMillis));
        log.trace("[BusinessMetrics] step success step={} latencyMs={}", stepName, latencyMillis);
    }

    /** 记录任务完成终态。 */
    public void recordTaskCompleted() {
        taskCompletedTotal.increment();
    }

    /** 记录任务失败终态。 */
    public void recordTaskFailed() {
        taskFailedTotal.increment();
    }

    /** SSE 连接建立时调用。 */
    public void sseConnected() {
        sseActive++;
    }

    /** SSE 连接关闭时调用。 */
    public void sseDisconnected() {
        sseActive = Math.max(0, sseActive - 1);
    }

    /** 记录一次 MQ 任务消息发布。 */
    public void recordMqPublished() {
        mqPublishedTotal.increment();
    }
}
