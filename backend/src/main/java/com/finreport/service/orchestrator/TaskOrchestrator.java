package com.finreport.service.orchestrator;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.r2dbc.core.DatabaseClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.reactive.TransactionalOperator;

import com.finreport.domain.entity.Report;
import com.finreport.domain.entity.Task;
import com.finreport.domain.entity.TaskStep;
import com.finreport.domain.enums.StepStatus;
import com.finreport.domain.enums.TaskStepName;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.exception.BusinessException;
import com.finreport.mq.TaskMessageProducer;
import com.finreport.repository.ReportRepository;
import com.finreport.repository.TaskRepository;
import com.finreport.repository.TaskStepRepository;
import com.finreport.service.artifact.ReportArtifactWriter;
import com.finreport.service.reasoner.CheckResultWriter;
import com.finreport.service.statement.StatementWriter;
import com.finreport.trace.TraceContext;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 任务编排器 — spec §2.2 M2。
 *
 * <p>负责将"解析一份财报"拆为 6 个原子任务（PARSE → EXTRACT_BS/IS/CF → CHECK → REPORT），
 * 通过 RabbitMQ 分发给 L3 AI 服务，并根据 progress 回报推进状态机。
 * M1.09: 骨架版本，含任务创建、步骤调度、基本 progress 处理。</p>
 */
@Service
public class TaskOrchestrator {

    private static final Logger log = LoggerFactory.getLogger(TaskOrchestrator.class);

    private static final String TASK_TYPE_REPORT = "REPORT_PARSE";
    private static final int PROGRESS_PARSE = 15;
    private static final int PROGRESS_EXTRACT = 55;
    private static final int PROGRESS_CHECK = 75;
    private static final int PROGRESS_REPORT = 100;

    private final TaskRepository taskRepo;
    private final TaskStepRepository stepRepo;
    private final TaskStateMachine stateMachine;
    private final TaskMessageProducer messageProducer;
    private final DatabaseClient databaseClient;
    private final TransactionalOperator transactionalOperator;
    private final ExtractDispatcher extractDispatcher;
    private final ExtractCompletionTracker extractTracker;
    private final StatementWriter statementWriter;
    private final ExtractCacheService extractCacheService;
    private final CheckResultWriter checkResultWriter;
    private final ReportArtifactWriter reportArtifactWriter;
    private final ReportRepository reportRepo;

    public TaskOrchestrator(
            TaskRepository taskRepo,
            TaskStepRepository stepRepo,
            TaskStateMachine stateMachine,
            TaskMessageProducer messageProducer,
            DatabaseClient databaseClient,
            TransactionalOperator transactionalOperator,
            ExtractDispatcher extractDispatcher,
            ExtractCompletionTracker extractTracker,
            StatementWriter statementWriter,
            ExtractCacheService extractCacheService,
            CheckResultWriter checkResultWriter,
            ReportArtifactWriter reportArtifactWriter,
            ReportRepository reportRepo) {
        this.taskRepo = taskRepo;
        this.stepRepo = stepRepo;
        this.stateMachine = stateMachine;
        this.messageProducer = messageProducer;
        this.databaseClient = databaseClient;
        this.transactionalOperator = transactionalOperator;
        this.extractDispatcher = extractDispatcher;
        this.extractTracker = extractTracker;
        this.statementWriter = statementWriter;
        this.extractCacheService = extractCacheService;
        this.checkResultWriter = checkResultWriter;
        this.reportArtifactWriter = reportArtifactWriter;
        this.reportRepo = reportRepo;
    }

    /**
     * 创建新任务并调度首步（PARSE）。
     *
     * @param userId  用户 ID
     * @param payload 任务参数（jsonObjectKey, companyCode 等）
     * @return 创建的任务实体
     */
    public Mono<Task> createTask(Long userId, Map<String, Object> payload) {
        return createTask(userId, null, payload);
    }

    /**
     * 创建新任务并调度首步（PARSE），指定 refReportId。
     *
     * @param userId      用户 ID
     * @param refReportId 关联的报告 ID（可为 null）
     * @param payload     任务参数
     * @return 创建的任务实体
     */
    public Mono<Task> createTask(Long userId, Long refReportId, Map<String, Object> payload) {
        log.debug("[TaskOrchestrator] createTask userId={} refReportId={}", userId, refReportId);

        String taskId = generateTaskId();
        Task task = buildTask(taskId, userId, refReportId, payload);

        return initializeTask(task)
                .then(dispatchStep(taskId, TaskStepName.PARSE, payload));
    }

    /**
     * 更新任务的 refReportId（报告创建后回写）。
     *
     * @param taskId   任务 ID
     * @param reportId 报告 ID
     * @return 更新后的 Task
     */
    public Mono<Task> updateRefReportId(String taskId, Long reportId) {
        log.debug("[TaskOrchestrator] updateRefReportId taskId={} reportId={}", taskId, reportId);
        return taskRepo.findById(taskId)
                .switchIfEmpty(Mono.error(new BusinessException(
                        org.springframework.http.HttpStatus.NOT_FOUND,
                        "TASK_NOT_FOUND", "任务不存在: " + taskId)))
                .flatMap(task -> {
                    task.setRefReportId(reportId);
                    return taskRepo.save(task);
                });
    }

    /**
     * 将任务标记为 FAILED（用于处理上传过程中 MinIO/DB 失败）。
     *
     * <p>此方法仅设置状态和错误信息，不影响 task_step 记录。
     * 仅对非终态任务生效。</p>
     *
     * @param taskId   任务 ID
     * @param errorMsg 错误描述
     * @return 更新后的 Task（若任务不存在则返回 empty）
     */
    public Mono<Task> markTaskFailed(String taskId, String errorMsg) {
        log.debug("[TaskOrchestrator] markTaskFailed taskId={} error={}", taskId, errorMsg);
        return taskRepo.findById(taskId)
                .flatMap(task -> {
                    TaskStatus current = TaskStatus.valueOf(task.getStatus());
                    if (current.isTerminal()) {
                        return Mono.just(task);
                    }
                    task.setStatus(TaskStatus.FAILED.name());
                    task.setErrorMsg(errorMsg);
                    task.setFinishedAt(LocalDateTime.now());
                    return taskRepo.save(task);
                });
    }

    /**
     * 处理 L3 回报的步骤进度。
     *
     * <p>根据 step 和 status 推进任务状态机。
     * EXTRACT_BS/IS/CF 三条都 SUCCESS 后才触发 CHECK。</p>
     *
     * @param taskId   任务 ID
     * @param stepName 步骤名称
     * @param status   步骤状态（SUCCESS / FAILED）
     * @param result   步骤结果（可选）
     * @return 更新后的 Task
     */
    public Mono<Task> handleStepProgress(
            String taskId, String stepName, String status, Map<String, Object> result) {
        log.debug("[TaskOrchestrator] handleStepProgress taskId={} step={} status={}",
                taskId, stepName, status);

        return taskRepo.findById(taskId)
                .switchIfEmpty(Mono.error(new BusinessException(
                        org.springframework.http.HttpStatus.NOT_FOUND,
                        "TASK_NOT_FOUND", "任务不存在: " + taskId)))
                .flatMap(task -> {
                    TaskStatus currentStatus = TaskStatus.valueOf(task.getStatus());

                    if (currentStatus.isTerminal()) {
                        log.debug("[TaskOrchestrator] 任务已终态，忽略 progress taskId={} status={}",
                                taskId, currentStatus);
                        return Mono.just(task);
                    }

                    if ("SUCCESS".equalsIgnoreCase(status)) {
                        return handleStepSuccess(task, stepName, result);
                    } else if ("FAILED".equalsIgnoreCase(status)) {
                        return handleStepFailure(task, stepName, result);
                    } else {
                        log.debug("[TaskOrchestrator] 未知步骤状态 taskId={} step={} status={}",
                                taskId, stepName, status);
                        return Mono.just(task);
                    }
                });
    }

    /**
     * 取消任务。
     *
     * @param taskId 任务 ID
     * @return 更新后的 Task
     */
    public Mono<Task> cancelTask(String taskId) {
        log.debug("[TaskOrchestrator] cancelTask taskId={}", taskId);
        return taskRepo.findById(taskId)
                .switchIfEmpty(Mono.error(new BusinessException(
                        org.springframework.http.HttpStatus.NOT_FOUND,
                        "TASK_NOT_FOUND", "任务不存在: " + taskId)))
                .flatMap(this::cancelExistingTask);
    }

    private Mono<Task> cancelExistingTask(Task task) {
        TaskStatus current = TaskStatus.valueOf(task.getStatus());
        if (current.isTerminal()) {
            return Mono.just(task);
        }
        if (!stateMachine.canTransition(current, TaskStatus.CANCELLED)) {
            return Mono.error(new BusinessException(
                    org.springframework.http.HttpStatus.CONFLICT,
                    "INVALID_TRANSITION", "当前状态不允许取消: " + current));
        }
        task.setStatus(TaskStatus.CANCELLED.name());
        task.setFinishedAt(LocalDateTime.now());
        return taskRepo.save(task)
                .doOnSuccess(saved -> log.info("[TaskOrchestrator] 任务已取消 taskId={}", task.getId()));
    }

    /**
     * 按 ID 查询任务。
     *
     * @param taskId 任务 ID
     * @return 任务实体（可能为 empty）
     */
    public Mono<Task> findById(String taskId) {
        log.debug("[TaskOrchestrator] findById taskId={}", taskId);
        return taskRepo.findById(taskId);
    }

    /**
     * 在用户边界内查询任务。
     *
     * @param taskId 任务 ID
     * @param userId 当前认证用户 ID
     * @return 所属任务；非所有者返回 empty
     */
    public Mono<Task> findByIdAndUserId(String taskId, Long userId) {
        log.debug("[TaskOrchestrator] findByIdAndUserId taskId={} userId={}", taskId, userId);
        return taskRepo.findByIdAndUserId(taskId, userId);
    }

    /**
     * 在用户边界内取消任务。
     *
     * @param taskId 任务 ID
     * @param userId 当前认证用户 ID
     * @return 已取消的任务
     */
    public Mono<Task> cancelTask(String taskId, Long userId) {
        log.debug("[TaskOrchestrator] cancelTask taskId={} userId={}", taskId, userId);
        return taskRepo.findByIdAndUserId(taskId, userId)
                .switchIfEmpty(Mono.error(new BusinessException(
                        org.springframework.http.HttpStatus.NOT_FOUND,
                        "TASK_NOT_FOUND", "任务不存在: " + taskId)))
                .flatMap(this::cancelExistingTask);
    }

    /**
     * 查询用户任务列表（按创建时间倒序）。
     *
     * @param userId 用户 ID
     * @return 任务列表
     */
    public Flux<Task> findByUserId(Long userId) {
        log.debug("[TaskOrchestrator] findByUserId userId={}", userId);
        return taskRepo.findByUserIdOrderByCreatedAtDesc(userId);
    }

    /**
     * 创建任务记录和步骤记录但不调度首步（用于文件上传场景，需等 MinIO 上传完成
     * 后再调用 {@link #dispatchTask}）。
     *
     * @param userId      用户 ID
     * @param refReportId 关联报告 ID（可为 null）
     * @param payload     任务参数
     * @return 创建的任务实体（含生成的 taskId）
     */
    public Mono<Task> createTaskWithoutDispatch(Long userId, Long refReportId, Map<String, Object> payload) {
        log.debug("[TaskOrchestrator] createTaskWithoutDispatch userId={} refReportId={}", userId, refReportId);

        String taskId = generateTaskId();
        Task task = buildTask(taskId, userId, refReportId, payload);

        return initializeTask(task)
                .thenReturn(task);
    }

    /**
     * 调度任务的首步（PARSE），通常在 MinIO 上传完成后调用。
     *
     * @param taskId 任务 ID
     * @return 更新后的 Task
     */
    public Mono<Task> dispatchTask(String taskId) {
        return dispatchTask(taskId, null);
    }

    /**
     * 调度任务的首步（PARSE），合并额外 payload 字段（如 pdfObjectKey）。
     *
     * @param taskId       任务 ID
     * @param extraPayload 额外字段（合并到已有 payload 中，可为 null）
     * @return 更新后的 Task
     */
    public Mono<Task> dispatchTask(String taskId, Map<String, Object> extraPayload) {
        log.debug("[TaskOrchestrator] dispatchTask taskId={}", taskId);
        return taskRepo.findById(taskId)
                .switchIfEmpty(Mono.error(new BusinessException(
                        org.springframework.http.HttpStatus.NOT_FOUND,
                        "TASK_NOT_FOUND", "任务不存在: " + taskId)))
                .flatMap(task -> {
                    Map<String, Object> payload = buildPayload(task);
                    if (extraPayload != null && !extraPayload.isEmpty()) {
                        payload = new java.util.LinkedHashMap<>(payload);
                        payload.putAll(extraPayload);
                    }
                    return dispatchStep(taskId, TaskStepName.PARSE, payload);
                });
    }

    // ========================================================================
    // Private helpers
    // ========================================================================

    private String generateTaskId() {
        return "task-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12);
    }

    /**
     * 构建 Task 实体（未持久化）。
     */
    private Task buildTask(String taskId, Long userId, Long refReportId, Map<String, Object> payload) {
        return Task.builder()
                .id(taskId)
                .userId(userId)
                .taskType(TASK_TYPE_REPORT)
                .refReportId(refReportId)
                .status(TaskStatus.PENDING.name())
                .currentStep(null)
                .progress(0)
                .payload(toJson(payload))
                .createdAt(LocalDateTime.now())
                .build();
    }

    /**
     * 原子创建 task 及全部初始 task_step 记录。
     *
     * <p>任务定义不完整时不应留下半成品 task；调度和 MQ 发布不属于该事务，
     * 由后续补偿逻辑处理发布失败。</p>
     */
    private Mono<Void> initializeTask(Task task) {
        return transactionalOperator.transactional(
                insertTask(task).then(createStepRecords(task.getId()).then()));
    }

    /**
     * 显式 INSERT（手动 ID 不可依赖 Repository.save 自动判断 INSERT/UPDATE）。
     */
    private Mono<Void> insertTask(Task task) {
        org.springframework.r2dbc.core.DatabaseClient.GenericExecuteSpec insert = databaseClient.sql(
                "INSERT INTO task (id, user_id, task_type, ref_report_id, status, current_step, progress, payload, created_at) "
                        + "VALUES (:id, :userId, :taskType, :refReportId, :status, :currentStep, :progress, :payload, :createdAt)")
                .bind("id", task.getId())
                .bind("userId", task.getUserId())
                .bind("taskType", task.getTaskType());

        insert = bindNullable(insert, "refReportId", task.getRefReportId(), Long.class)
                .bind("status", task.getStatus());
        insert = bindNullable(insert, "currentStep", task.getCurrentStep(), String.class)
                .bind("progress", task.getProgress())
                .bind("payload", task.getPayload())
                .bind("createdAt", task.getCreatedAt());

        return insert.then()
                .doOnSuccess(v -> log.info("[TaskOrchestrator] 任务已创建 taskId={}", task.getId()));
    }

    /**
     * 为 R2DBC SQL 参数绑定可空值。
     *
     * <p>{@code DatabaseClient.bind} 不接受 {@code null}；可空列必须携带 JDBC 类型调用
     * {@code bindNull}，否则上传首次创建任务时会在执行 INSERT 前失败。</p>
     */
    private org.springframework.r2dbc.core.DatabaseClient.GenericExecuteSpec bindNullable(
            org.springframework.r2dbc.core.DatabaseClient.GenericExecuteSpec spec,
            String parameterName,
            Object value,
            Class<?> parameterType) {
        return value == null ? spec.bindNull(parameterName, parameterType) : spec.bind(parameterName, value);
    }

    private Flux<TaskStep> createStepRecords(String taskId) {
        return Flux.fromArray(TaskStepName.values())
                .flatMap(stepName -> {
                    TaskStep step = TaskStep.builder()
                            .taskId(taskId)
                            .stepName(stepName.name())
                            .status(StepStatus.PENDING.name())
                            .build();
                    return stepRepo.save(step);
                });
    }

    private Mono<Task> dispatchStep(String taskId, TaskStepName step, Map<String, Object> payload) {
        return taskRepo.findById(taskId)
                .flatMap(task -> {
                    TaskStatus current = TaskStatus.valueOf(task.getStatus());
                    TaskStatus nextRunning = stateMachine.runningStateFor(step.name());

                    if (!stateMachine.canTransition(current, nextRunning)) {
                        return Mono.error(new BusinessException(
                                org.springframework.http.HttpStatus.CONFLICT,
                                "INVALID_TRANSITION",
                                String.format("无法从 %s 转换到 %s", current, nextRunning)));
                    }

                    // 更新任务状态为 RUNNING
                    task.setStatus(nextRunning.name());
                    task.setCurrentStep(step.name());
                    if (task.getStartedAt() == null) {
                        task.setStartedAt(LocalDateTime.now());
                    }

                    return taskRepo.save(task)
                            .flatMap(saved -> stepRepo.findByTaskIdAndStepName(taskId, step.name())
                                    .flatMap(stepRecord -> {
                                        stepRecord.setStatus(StepStatus.RUNNING.name());
                                        stepRecord.setStartedAt(LocalDateTime.now());
                                        return stepRepo.save(stepRecord)
                                                .then(Mono.deferContextual(context -> Mono.fromRunnable(() ->
                                                        messageProducer.publishTaskStep(
                                                                taskId,
                                                                step.getRoutingKey(),
                                                                payload,
                                                                context.getOrDefault(TraceContext.TRACE_ID, "")))))
                                                .thenReturn(saved)
                                                .onErrorResume(com.finreport.exception.IntegrationException.class,
                                                        error -> markDispatchFailed(saved, stepRecord, error)
                                                                .then(Mono.<Task>error(error)));
                                    }));
                });
    }

    /**
     * 补偿 MQ 发布失败：任务和当前步骤均明确转为 FAILED，随后将原集成异常传给调用方。
     */
    private Mono<Void> markDispatchFailed(
            Task task,
            TaskStep stepRecord,
            com.finreport.exception.IntegrationException error) {
        String message = "MQ 发布失败: " + error.getMessage();
        log.error("[TaskOrchestrator] MQ 发布失败，标记任务失败 taskId={} step={}",
                task.getId(), stepRecord.getStepName(), error);
        task.setStatus(TaskStatus.FAILED.name());
        task.setErrorMsg(message);
        task.setFinishedAt(LocalDateTime.now());
        stepRecord.setStatus(StepStatus.FAILED.name());
        stepRecord.setErrorMsg(message);
        stepRecord.setFinishedAt(LocalDateTime.now());
        return stepRepo.save(stepRecord).then(taskRepo.save(task)).then();
    }

    private Mono<Task> handleStepSuccess(Task task, String stepName, Map<String, Object> result) {
        String taskId = task.getId();
        log.debug("[TaskOrchestrator] 步骤成功 taskId={} step={}", taskId, stepName);

        // A duplicate SUCCESS must be acknowledged without changing state or triggering the next step.
        // idempotency_key is taskId + step, so progress redelivery cannot schedule downstream work twice.
        return stepRepo.findByTaskIdAndStepName(taskId, stepName)
                .switchIfEmpty(Mono.error(new BusinessException(
                        org.springframework.http.HttpStatus.NOT_FOUND,
                        "TASK_STEP_NOT_FOUND", "任务步骤不存在: " + stepName)))
                .flatMap(step -> {
                    if (StepStatus.SUCCESS.name().equals(step.getStatus())) {
                        log.warn("[TaskOrchestrator] 重放成功进度，补偿检查后续编排 taskId={} step={}",
                                taskId, stepName);
                        return reconcileSuccessfulStep(task, stepName, taskId);
                    }
                    step.setStatus(StepStatus.SUCCESS.name());
                    step.setFinishedAt(LocalDateTime.now());
                    if (step.getStartedAt() != null) {
                        step.setDurationMs((int) java.time.Duration.between(
                                step.getStartedAt(), LocalDateTime.now()).toMillis());
                    }
                    return stepRepo.save(step)
                            .then(updateTaskProgress(task, stepName))
                            .flatMap(updatedTask -> {
                                if (stepName.startsWith("EXTRACT_")) {
                                    // M2.09: persist extracted statement rows before triggering CHECK.
                                    // Write failures are logged inside StatementWriter but do not
                                    // block the state machine — a missing row surfaces as a CHECK
                                    // rule failure downstream (spec §8.4 失败不强制回滚).
                                    // M2.10: cache the extract result by pdf_md5 + step so the next
                                    // upload of the same PDF can skip extract entirely (spec §3.10).
                                    return statementWriter.writeStatement(taskId, stepName, result)
                                            .then(storeExtractCache(taskId, TaskStepName.valueOf(stepName), result))
                                            .then(handleExtractStepSuccess(
                                                    updatedTask, taskId, TaskStepName.valueOf(stepName)));
                                }
                                if ("CHECK".equals(stepName)) {
                                    // M3.04: persist check rules + anomalies before triggering REPORT.
                                    // Write failures are logged inside CheckResultWriter but do not
                                    // block the state machine — a missing row surfaces as a REPORT
                                    // generation failure downstream (spec §8.4 失败不强制回滚).
                                    return checkResultWriter.writeCheckResult(taskId, result)
                                            .then(handleNonExtractStepSuccess(updatedTask, stepName, taskId));
                                }
                                if ("REPORT".equals(stepName)) {
                                    // M3.08: upload PDF/MD/PNG to MinIO + write report_artifact
                                    // before transitioning to COMPLETED. Write failures are logged
                                    // inside ReportArtifactWriter but do not block the state machine
                                    // — a missing artifact surfaces as a download failure downstream
                                    // (spec §8.4 失败不强制回滚).
                                    return reportArtifactWriter.writeArtifacts(taskId, result)
                                            .then(handleNonExtractStepSuccess(updatedTask, stepName, taskId));
                                }
                                return handleNonExtractStepSuccess(updatedTask, stepName, taskId);
                            });
                });
    }

    /**
     * Reconcile downstream dispatch after a redelivered successful progress event.
     *
     * <p>A process crash can occur after the current step reaches SUCCESS but before its
     * downstream message is published. Replaying the stable idempotency key therefore repairs
     * missing PENDING steps instead of silently returning and leaving the task stalled.</p>
     */
    private Mono<Task> reconcileSuccessfulStep(Task task, String stepName, String taskId) {
        TaskStatus current = TaskStatus.valueOf(task.getStatus());
        return switch (stepName) {
            case "PARSE" -> current == TaskStatus.PARSE_SUCCESS || current == TaskStatus.EXTRACT_RUNNING
                    ? dispatchExtractionSteps(task, buildPayload(task))
                    : Mono.just(task);
            case "EXTRACT_BS", "EXTRACT_IS", "EXTRACT_CF" ->
                    reconcileExtractSuccess(task, taskId, current);
            case "CHECK" -> current == TaskStatus.CHECK_RUNNING || current == TaskStatus.CHECK_SUCCESS
                    ? handleNonExtractStepSuccess(task, stepName, taskId)
                    : Mono.just(task);
            case "REPORT" -> current == TaskStatus.REPORT_RUNNING || current == TaskStatus.REPORT_SUCCESS
                    ? handleNonExtractStepSuccess(task, stepName, taskId)
                    : Mono.just(task);
            default -> Mono.just(task);
        };
    }

    private Mono<Task> reconcileExtractSuccess(Task task, String taskId, TaskStatus current) {
        if (current != TaskStatus.EXTRACT_RUNNING
                && current != TaskStatus.EXTRACT_PARTIAL
                && current != TaskStatus.EXTRACT_SUCCESS) {
            return Mono.just(task);
        }
        // M2 review fix (Blocker F): step SUCCESS 但 statement 可能未写入(进程崩溃在 step.save
        // 之后、statementWriter.writeStatement 之前)。reconcile 路径必须先补写 statement,
        // 否则 CHECK 阶段因数据缺失失败。从 ExtractCacheService 拿 fallback result 重写。
        return ensureStatementsWritten(task, taskId)
                .then(Mono.defer(() -> {
                    // Reconcile path intentionally bypasses the Redis tracker: redelivered SUCCESS events
                    // cannot reconstruct per-step recordSuccess calls, so MySQL remains source of truth.
                    return checkAllExtractsDone(taskId);
                }))
                .flatMap(allDone -> {
                    if (!Boolean.TRUE.equals(allDone)) {
                        return Mono.just(task);
                    }
                    if (current == TaskStatus.EXTRACT_SUCCESS) {
                        return dispatchStepIfPending(task, TaskStepName.CHECK, buildPayload(task));
                    }
                    task.setStatus(TaskStatus.EXTRACT_SUCCESS.name());
                    task.setProgress(PROGRESS_EXTRACT);
                    return taskRepo.save(task)
                            .flatMap(saved -> dispatchStepIfPending(
                                    saved, TaskStepName.CHECK, buildPayload(saved)));
                });
    }

    /**
     * 对 3 个 EXTRACT step 并行补写 statement（幂等）。
     *
     * <p>M2 review fix (Blocker F): 从 reportRepo 拿 pdfMd5,对每个 EXTRACT step
     * 调 {@link ExtractCacheService#lookup} 拿 fallback result,
     * 再调 {@link StatementWriter#ensureStatementWritten} 检查 + 补写。
     * report 不存在 / pdfMd5 为空 / cache miss / fsRepo 检查失败均静默跳过,
     * CHECK 阶段会因数据缺失自然失败暴露问题(spec §8.4)。</p>
     */
    private Mono<Void> ensureStatementsWritten(Task task, String taskId) {
        return reportRepo.findByTaskId(taskId)
                .flatMap(report -> {
                    String pdfMd5 = report.getPdfMd5();
                    if (pdfMd5 == null || pdfMd5.isBlank()) {
                        log.debug("[TaskOrchestrator] ensureStatementsWritten pdfMd5 为空,跳过 taskId={}", taskId);
                        return Mono.<Void>empty();
                    }
                    return Flux.fromIterable(ExtractCacheService.EXTRACTION_STEPS)
                            .flatMap(step -> extractCacheService.lookup(pdfMd5, step)
                                    .flatMap(cachedResult -> statementWriter.ensureStatementWritten(
                                            taskId, step.name(), cachedResult))
                                    .onErrorResume(error -> {
                                        log.warn("[TaskOrchestrator] ensureStatementsWritten 单步失败 taskId={} step={}",
                                                taskId, step.name(), error);
                                        return Mono.just(0);
                                    }))
                            .then();
                })
                .onErrorResume(error -> {
                    log.warn("[TaskOrchestrator] ensureStatementsWritten 整体失败 taskId={}", taskId, error);
                    return Mono.empty();
                });
    }

    /**
     * Handle a successful EXTRACT_BS / EXTRACT_IS / EXTRACT_CF progress event.
     *
     * <p>Hot path: Redis {@link ExtractCompletionTracker#recordSuccess} returns the
     * current success count atomically; when count reaches
     * {@link ExtractCompletionTracker#EXPECTED_COUNT} and no failed flag is set,
     * transition to {@code EXTRACT_SUCCESS} and dispatch CHECK without touching MySQL
     * {@code task_step} for the completion check (spec §3.2.1 AtomicInteger fast path).</p>
     *
     * <p>Fallback: Redis unavailable or hot path ambiguous (count &lt; EXPECTED_COUNT
     * or failed flag set) → fall back to {@link #checkAllExtractsDone} MySQL reconcile.</p>
     */
    private Mono<Task> handleExtractStepSuccess(Task updatedTask, String taskId, TaskStepName step) {
        return extractTracker.recordSuccess(taskId, step)
                .flatMap(count -> extractTracker.hasFailed(taskId)
                        .flatMap(failed -> {
                            if (count >= ExtractCompletionTracker.EXPECTED_COUNT && !failed) {
                                return transitionToExtractSuccess(updatedTask);
                            }
                            return reconcileExtractViaMysql(updatedTask, taskId);
                        }));
    }

    private Mono<Task> transitionToExtractSuccess(Task updatedTask) {
        updatedTask.setStatus(TaskStatus.EXTRACT_SUCCESS.name());
        updatedTask.setProgress(PROGRESS_EXTRACT);
        return taskRepo.save(updatedTask)
                .flatMap(saved -> dispatchStepIfPending(
                        saved, TaskStepName.CHECK, buildPayload(saved)));
    }

    private Mono<Task> reconcileExtractViaMysql(Task updatedTask, String taskId) {
        return checkAllExtractsDone(taskId)
                .flatMap(allDone -> {
                    if (Boolean.TRUE.equals(allDone)) {
                        return transitionToExtractSuccess(updatedTask);
                    }
                    updatedTask.setStatus(TaskStatus.EXTRACT_PARTIAL.name());
                    return taskRepo.save(updatedTask);
                });
    }

    private Mono<Task> handleNonExtractStepSuccess(Task updatedTask, String stepName, String taskId) {
        TaskStatus newStatus = stateMachine.onStepSuccess(stepName);
        updatedTask.setStatus(newStatus.name());
        return taskRepo.save(updatedTask)
                .flatMap(saved -> {
                    // PARSE 完成后，三个报表抽取步骤必须一起进入 RUNNING 并分别投递。
                    if (TaskStepName.PARSE.name().equals(stepName)) {
                        return dispatchExtractionSteps(saved, buildPayload(saved));
                    }
                    // REPORT 是最后一个步骤，成功后自动转为 COMPLETED
                    if (newStatus == TaskStatus.REPORT_SUCCESS) {
                        saved.setStatus(TaskStatus.COMPLETED.name());
                        saved.setFinishedAt(LocalDateTime.now());
                        saved.setProgress(PROGRESS_REPORT);
                        return taskRepo.save(saved);
                    }
                    String next = stateMachine.nextStepAfter(stepName);
                    if (next != null) {
                        return dispatchStepIfPending(saved,
                                TaskStepName.valueOf(next),
                                buildPayload(saved));
                    }
                    return Mono.just(saved);
                });
    }

    /**
     * Dispatch a downstream step only when its progress has not already been scheduled.
     *
     * <p>This makes a replayed upstream SUCCESS safe when the original downstream publish
     * already completed.</p>
     */
    private Mono<Task> dispatchStepIfPending(
            Task task, TaskStepName step, Map<String, Object> payload) {
        return stepRepo.findByTaskIdAndStepName(task.getId(), step.name())
                .switchIfEmpty(Mono.error(new BusinessException(
                        org.springframework.http.HttpStatus.NOT_FOUND,
                        "TASK_STEP_NOT_FOUND", "任务步骤不存在: " + step.name())))
                .flatMap(stepRecord -> StepStatus.PENDING.name().equals(stepRecord.getStatus())
                        ? dispatchStep(task.getId(), step, payload)
                        : Mono.just(task));
    }

    /**
     * 将三个报表抽取步骤统一切换到运行态并逐个发布到 RabbitMQ。
     *
     * <p>任务级状态只从 {@code PARSE_SUCCESS} 转换一次到 {@code EXTRACT_RUNNING}；
     * 之后委托 {@link ExtractDispatcher#dispatchAll} 完成 step 级 RUNNING 标记 + MQ 发布 +
     * Redis 计数器 reset（spec §3.2.1 AtomicInteger + M2.08）。</p>
     *
     * <p>M2.10: 在 {@link ExtractDispatcher#dispatchAll} 之前先查 {@link ExtractCacheService}
     * 三表全部命中即跳过 MQ 投递，重放 3 条 success 路径并直接调度 CHECK
     * （spec §3.10：同 pdf_md5 重传命中缓存，跳过 extract）。</p>
     */
    private Mono<Task> dispatchExtractionSteps(Task task, Map<String, Object> payload) {
        TaskStatus current = TaskStatus.valueOf(task.getStatus());
        if (current == TaskStatus.EXTRACT_RUNNING) {
            return checkCacheAndReplayOrDispatch(task, payload);
        }
        if (!stateMachine.canTransition(current, TaskStatus.EXTRACT_RUNNING)) {
            return Mono.error(new BusinessException(
                    org.springframework.http.HttpStatus.CONFLICT,
                    "INVALID_TRANSITION",
                    String.format("无法从 %s 转换到 %s", current, TaskStatus.EXTRACT_RUNNING)));
        }

        task.setStatus(TaskStatus.EXTRACT_RUNNING.name());
        task.setCurrentStep(TaskStepName.EXTRACT_BS.name());
        return taskRepo.save(task)
                .flatMap(saved -> checkCacheAndReplayOrDispatch(saved, payload));
    }

    /**
     * 查 extract 缓存：三表全部命中则重放缓存结果并直接调度 CHECK；否则走 MQ 投递。
     *
     * <p>Report 不存在（找不到 pdf_md5 关联）或 Redis 故障时静默 fallback 到
     * {@link ExtractDispatcher#dispatchAll}（spec §3.10 失败策略）。
     * 部分命中（size &lt; 3）也走 MQ，避免半缓存导致数据不一致。</p>
     */
    private Mono<Task> checkCacheAndReplayOrDispatch(Task task, Map<String, Object> payload) {
        String taskId = task.getId();
        return reportRepo.findByTaskId(taskId)
                .flatMap(report -> extractCacheService.lookupAll(report.getPdfMd5())
                        .flatMap(cached -> {
                            if (cached.size() == ExtractCacheService.EXTRACTION_STEPS.size()) {
                                log.info("[TaskOrchestrator] extract 缓存全部命中，跳过 MQ taskId={} pdfMd5={}",
                                        taskId, report.getPdfMd5());
                                return replayCachedExtracts(task, cached);
                            }
                            log.debug("[TaskOrchestrator] extract 缓存部分命中 ({}/3)，走 MQ taskId={} pdfMd5={}",
                                    cached.size(), taskId, report.getPdfMd5());
                            return extractDispatcher.dispatchAll(task, payload).thenReturn(task);
                        }))
                .switchIfEmpty(Mono.defer(() -> {
                    log.debug("[TaskOrchestrator] 无 report 关联，直接走 MQ extract taskId={}", taskId);
                    return extractDispatcher.dispatchAll(task, payload).thenReturn(task);
                }));
    }

    /**
     * 重放 3 条 extract 缓存结果：逐条标记 step SUCCESS + 调 StatementWriter 写库，
     * 然后复用 {@link #transitionToExtractSuccess} 跳到 CHECK 调度。
     *
     * <p>缓存重放路径不调 {@link ExtractCompletionTracker}（计数器专为 MQ 回报设计）；
     * 也不调 {@code extractCacheService.store}，避免重复写缓存。</p>
     *
     * <p><b>M2 review fix (Blocker B): 幂等短路</b> — 若 step 已是 SUCCESS（极端并发场景
     * 下同 taskId 被触发两次 replay），跳过 setStatus / save / writeStatement，避免重复
     * 写 financial_statement 行造成数据膨胀。已成功 step 视为已写入，直接进入下一个。</p>
     */
    private Mono<Task> replayCachedExtracts(
            Task task, Map<TaskStepName, Map<String, Object>> cached) {
        String taskId = task.getId();
        return Flux.fromIterable(ExtractCacheService.EXTRACTION_STEPS)
                .concatMap(step -> stepRepo.findByTaskIdAndStepName(taskId, step.name())
                        .switchIfEmpty(Mono.error(new BusinessException(
                                org.springframework.http.HttpStatus.NOT_FOUND,
                                "TASK_STEP_NOT_FOUND", "任务步骤不存在: " + step.name())))
                        .flatMap(stepRecord -> {
                            if (StepStatus.SUCCESS.name().equals(stepRecord.getStatus())) {
                                log.info("[TaskOrchestrator] replay 短路：step 已 SUCCESS，跳过写库 taskId={} step={}",
                                        taskId, step.name());
                                return Mono.just(stepRecord);
                            }
                            stepRecord.setStatus(StepStatus.SUCCESS.name());
                            stepRecord.setFinishedAt(LocalDateTime.now());
                            Map<String, Object> cachedResult = cached.get(step);
                            return stepRepo.save(stepRecord)
                                    .then(statementWriter.writeStatement(taskId, step.name(), cachedResult))
                                    .thenReturn(stepRecord);
                        }))
                .then(transitionToExtractSuccess(task));
    }

    /**
     * 写入 extract 缓存（pdf_md5 + step → result）。
     *
     * <p>由 {@link #handleStepSuccess} 在 L3 extract progress SUCCESS 后调用。
     * Report 找不到或 pdf_md5 为空时跳过缓存写入（{@link ExtractCacheService#store}
     * 内部对 null pdfMd5 返回空 Mono）；Redis 故障在 cache 层已静默吞掉。</p>
     */
    private Mono<Void> storeExtractCache(String taskId, TaskStepName step, Map<String, Object> result) {
        return reportRepo.findByTaskId(taskId)
                .flatMap(report -> extractCacheService.store(report.getPdfMd5(), step, result))
                .onErrorResume(error -> {
                    log.warn("[TaskOrchestrator] storeExtractCache 失败 taskId={} step={}",
                            taskId, step, error);
                    return Mono.empty();
                });
    }

    /**
     * 检查某个任务的所有抽取步骤是否都已完成。
     *
     * <p>三个步骤必须都存在且状态为 SUCCESS，缺少任何一条记录均视为未完成。</p>
     */
    private Mono<Boolean> checkAllExtractsDone(String taskId) {
        List<String> extractSteps = List.of("EXTRACT_BS", "EXTRACT_IS", "EXTRACT_CF");
        return Flux.fromIterable(extractSteps)
                .flatMap(name -> stepRepo.findByTaskIdAndStepName(taskId, name)
                        .switchIfEmpty(Mono.defer(() -> {
                            TaskStep missing = new TaskStep();
                            missing.setStatus("MISSING");
                            return Mono.just(missing);
                        })))
                .all(step -> StepStatus.SUCCESS.name().equals(step.getStatus()));
    }

    private Mono<Task> handleStepFailure(Task task, String stepName, Map<String, Object> result) {
        String taskId = task.getId();
        log.debug("[TaskOrchestrator] 步骤失败 taskId={} step={}", taskId, stepName);
        // EXTRACT_* failure sets Redis failed flag so the hot-path CHECK trigger in
        // handleExtractStepSuccess is blocked until retry clears it via clearFailure.
        Mono<Void> recordFailure = stepName.startsWith("EXTRACT_")
                ? extractTracker.recordFailure(taskId, TaskStepName.valueOf(stepName))
                : Mono.empty();
        return recordFailure.then(stepRepo.findByTaskIdAndStepName(taskId, stepName)
                .switchIfEmpty(Mono.error(new BusinessException(
                        org.springframework.http.HttpStatus.NOT_FOUND,
                        "TASK_STEP_NOT_FOUND", "任务步骤不存在: " + stepName)))
                .flatMap(step -> scheduleRetryOrFail(task, step, result)));
    }

    private Mono<Task> scheduleRetryOrFail(Task task, TaskStep step, Map<String, Object> result) {
        int retries = step.getRetryCount() == null ? 0 : step.getRetryCount();
        String taskId = task.getId();
        String stepName = step.getStepName();
        boolean isExtract = stepName.startsWith("EXTRACT_");
        if (retries >= TaskStateMachine.MAX_RETRIES) {
            step.setStatus(StepStatus.FAILED.name());
            step.setFinishedAt(LocalDateTime.now());
            step.setErrorMsg(errorMessage(result));
            task.setStatus(TaskStatus.FAILED.name());
            task.setFinishedAt(LocalDateTime.now());
            task.setErrorMsg("步骤 " + stepName + " 重试耗尽");
            // Terminal FAILED: leave the failed flag set so any late-arriving sibling SUCCESS
            // cannot trigger CHECK via the hot path.
            return stepRepo.save(step).then(taskRepo.save(task));
        }

        int nextRetry = retries + 1;
        TaskStatus retryStatus = stateMachine.decideRetryOrFail(
                stateMachine.onStepFailure(stepName), retries);
        step.setStatus(StepStatus.RETRY.name());
        step.setRetryCount(nextRetry);
        step.setErrorMsg(errorMessage(result));
        task.setStatus(retryStatus.name());
        // M2 review fix: clearFailure MUST happen AFTER publishRetry succeeds.
        // 之前先 clearFailure 再 publishRetry,若另两个 step 的 SUCCESS 在此窗口回报,
        // hasFailed=false + recordSuccess 推到 3 → hot path 触发 CHECK,但本 step 在
        // MySQL 仍是 RETRY、statement 行缺失,CHECK 阶段会因数据不一致失败。
        // 现在改为 publishRetry 成功后才 clearFailure;publishRetry 失败走
        // markDispatchFailed 标记 task FAILED 并重新抛错。
        return stepRepo.save(step)
                .then(taskRepo.save(task))
                .flatMap(saved -> Mono.deferContextual(context -> Mono.fromCallable(() -> {
                    String traceId = context.getOrDefault(TraceContext.TRACE_ID, org.slf4j.MDC.get("traceId"));
                    messageProducer.publishRetry(
                            taskId,
                            TaskStepName.valueOf(stepName).getRoutingKey(),
                            buildPayload(saved),
                            nextRetry,
                            traceId);
                    return saved;
                })))
                .flatMap(saved -> isExtract
                        ? extractTracker.clearFailure(taskId).thenReturn(saved)
                        : Mono.just(saved))
                .onErrorResume(com.finreport.exception.IntegrationException.class,
                        error -> markDispatchFailed(task, step, error)
                                .then(Mono.<Task>error(error)));
    }

    private static String errorMessage(Map<String, Object> result) {
        if (result == null || result.get("error") == null) {
            return "步骤处理失败";
        }
        return String.valueOf(result.get("error"));
    }

    private Mono<Task> updateTaskProgress(Task task, String stepName) {
        int progress = switch (stepName) {
            case "PARSE" -> PROGRESS_PARSE;
            case "EXTRACT_BS", "EXTRACT_IS", "EXTRACT_CF" -> PROGRESS_EXTRACT;
            case "CHECK" -> PROGRESS_CHECK;
            case "REPORT" -> PROGRESS_REPORT;
            default -> task.getProgress() != null ? task.getProgress() : 0;
        };
        task.setProgress(progress);
        return Mono.just(task);
    }

    private Map<String, Object> buildPayload(Task task) {
        if (task.getPayload() == null) {
            return Map.of();
        }
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().readValue(
                    task.getPayload(), Map.class);
        } catch (Exception e) {
            return Map.of("raw", task.getPayload());
        }
    }

    private String toJson(Map<String, Object> payload) {
        if (payload == null) return null;
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().writeValueAsString(payload);
        } catch (Exception e) {
            log.warn("[TaskOrchestrator] JSON 序列化失败", e);
            return null;
        }
    }
}
