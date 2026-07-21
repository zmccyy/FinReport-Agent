package com.finreport.service.orchestrator;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import com.finreport.domain.entity.Task;
import com.finreport.domain.entity.TaskStep;
import com.finreport.domain.enums.StepStatus;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.domain.enums.TaskStepName;
import com.finreport.exception.BusinessException;
import com.finreport.exception.IntegrationException;
import com.finreport.mq.TaskMessageProducer;
import com.finreport.repository.TaskRepository;
import com.finreport.repository.TaskStepRepository;
import com.finreport.trace.TraceContext;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 三表抽取消息分发器 — spec §3.2.1 + plan M2.08。
 *
 * <p>负责把 PARSE_SUCCESS 后的任务并发发布为 3 条 extract 消息
 * （{@code extract.bs / extract.is / extract.cf}），同时把对应的
 * {@code task_step} 记录从 {@code PENDING} 标记为 {@code RUNNING}。</p>
 *
 * <p>从 {@link TaskOrchestrator} 抽出，独立可测、可复用：
 * {@link #dispatchAll} 用于 PARSE_SUCCESS → EXTRACT_RUNNING 路径；
 * {@link #dispatchSingle} 用于 EXTRACT_RETRY 重新发布单条失败的 step。</p>
 *
 * <p>发布前先调用 {@link ExtractCompletionTracker#reset} 清理计数器，
 * 避免 reconcile 重放或重试场景下残留旧计数。MQ 发布失败时把
 * task + step 同时标记为 FAILED（与原 {@code markDispatchFailed} 行为一致），
 * 然后把 {@link IntegrationException} 传给调用方决定是否上抛。</p>
 *
 * <p>三条消息按 {@code concatMap} 顺序发布（而非 {@code flatMap} 并发）：
 * RabbitMQ 生产者侧本身是单连接多路复用，顺序发布可确保 traceId/header
 * 透传语义清晰；三表互不依赖，发布顺序不影响 worker 侧并行消费。</p>
 */
@Component
public class ExtractDispatcher {

    private static final Logger log = LoggerFactory.getLogger(ExtractDispatcher.class);

    /** 三表抽取步骤固定顺序：BS → IS → CF（与 spec §3.2 描述顺序一致）。 */
    public static final List<TaskStepName> EXTRACTION_STEPS = List.of(
            TaskStepName.EXTRACT_BS,
            TaskStepName.EXTRACT_IS,
            TaskStepName.EXTRACT_CF);

    private final TaskRepository taskRepo;
    private final TaskStepRepository stepRepo;
    private final TaskMessageProducer messageProducer;
    private final ExtractCompletionTracker tracker;

    public ExtractDispatcher(
            TaskRepository taskRepo,
            TaskStepRepository stepRepo,
            TaskMessageProducer messageProducer,
            ExtractCompletionTracker tracker) {
        this.taskRepo = taskRepo;
        this.stepRepo = stepRepo;
        this.messageProducer = messageProducer;
        this.tracker = tracker;
    }

    /**
     * 清理计数器并顺序发布三条 extract 消息。
     *
     * <p>调用方负责把 task 状态从 {@code PARSE_SUCCESS} 转为 {@code EXTRACT_RUNNING}；
     * 本方法只负责 step 级 RUNNING 标记 + MQ 发布。</p>
     *
     * @param task    已处于 {@code EXTRACT_RUNNING} 的任务
     * @param payload 步骤参数（pdfObjectKey / reportPeriod 等）
     * @return 完成信号；任意一条 MQ 发布失败会通过 {@link IntegrationException} 上抛
     */
    public Mono<Void> dispatchAll(Task task, Map<String, Object> payload) {
        log.debug("[ExtractDispatcher] dispatchAll taskId={}", task.getId());
        return tracker.reset(task.getId())
                .thenMany(Flux.fromIterable(EXTRACTION_STEPS)
                        .concatMap(step -> markRunningAndPublish(task, step, payload)))
                .then();
    }

    /**
     * 重新发布单条 extract 消息（用于 EXTRACT_RETRY 路径）。
     *
     * <p>调用方负责把 step 状态从 {@code FAILED} 转为 {@code RETRY}、
     * 增加 {@code retry_count}；本方法重新标记 {@code RUNNING} 并 publish。
     * 发布前调用 {@link ExtractCompletionTracker#clearFailure} 清除失败标记，
     * 避免重试成功后 CHECK 调度条件仍被 failed flag 阻断。</p>
     *
     * @param task    当前任务
     * @param step    要重试的抽取步骤（BS / IS / CF）
     * @param payload 步骤参数
     * @return 完成信号
     */
    public Mono<Void> dispatchSingle(Task task, TaskStepName step, Map<String, Object> payload) {
        log.debug("[ExtractDispatcher] dispatchSingle taskId={} step={}", task.getId(), step);
        return tracker.clearFailure(task.getId())
                .then(markRunningAndPublish(task, step, payload));
    }

    /**
     * 把单条 step 标 RUNNING 并发布 MQ 消息。
     *
     * <p>仅当 step 当前状态为 {@code PENDING} 时才发布，支持 reconcile 重放：
     * 重复 progress 不会触发重复发布。</p>
     */
    private Mono<Void> markRunningAndPublish(Task task, TaskStepName step, Map<String, Object> payload) {
        return stepRepo.findByTaskIdAndStepName(task.getId(), step.name())
                .switchIfEmpty(Mono.error(new BusinessException(
                        HttpStatus.NOT_FOUND,
                        "TASK_STEP_NOT_FOUND", "任务步骤不存在: " + step.name())))
                .flatMap(stepRecord -> {
                    if (!StepStatus.PENDING.name().equals(stepRecord.getStatus())) {
                        return Mono.empty();
                    }
                    stepRecord.setStatus(StepStatus.RUNNING.name());
                    stepRecord.setStartedAt(LocalDateTime.now());
                    return stepRepo.save(stepRecord)
                            .then(Mono.deferContextual(context -> Mono.<Void>fromRunnable(() ->
                                    messageProducer.publishTaskStep(
                                            task.getId(),
                                            step.getRoutingKey(),
                                            payload,
                                            context.getOrDefault(TraceContext.TRACE_ID, "")))))
                            .onErrorResume(IntegrationException.class,
                                    error -> markDispatchFailed(task, stepRecord, error)
                                            .then(Mono.<Void>error(error)));
                });
    }

    /**
     * 补偿 MQ 发布失败：任务和当前步骤均明确转为 FAILED，随后将原集成异常传给调用方。
     */
    private Mono<Void> markDispatchFailed(Task task, TaskStep stepRecord, IntegrationException error) {
        String message = "MQ 发布失败: " + error.getMessage();
        log.error("[ExtractDispatcher] MQ 发布失败，标记任务失败 taskId={} step={}",
                task.getId(), stepRecord.getStepName(), error);
        task.setStatus(TaskStatus.FAILED.name());
        task.setErrorMsg(message);
        task.setFinishedAt(LocalDateTime.now());
        stepRecord.setStatus(StepStatus.FAILED.name());
        stepRecord.setErrorMsg(message);
        stepRecord.setFinishedAt(LocalDateTime.now());
        return stepRepo.save(stepRecord).then(taskRepo.save(task)).then();
    }
}
