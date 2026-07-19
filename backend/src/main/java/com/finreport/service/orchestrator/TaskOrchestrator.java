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

import com.finreport.domain.entity.Task;
import com.finreport.domain.entity.TaskStep;
import com.finreport.domain.enums.StepStatus;
import com.finreport.domain.enums.TaskStepName;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.exception.BusinessException;
import com.finreport.mq.TaskMessageProducer;
import com.finreport.repository.TaskRepository;
import com.finreport.repository.TaskStepRepository;
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
    private static final List<TaskStepName> EXTRACTION_STEPS = List.of(
            TaskStepName.EXTRACT_BS,
            TaskStepName.EXTRACT_IS,
            TaskStepName.EXTRACT_CF);

    private final TaskRepository taskRepo;
    private final TaskStepRepository stepRepo;
    private final TaskStateMachine stateMachine;
    private final TaskMessageProducer messageProducer;
    private final DatabaseClient databaseClient;
    private final TransactionalOperator transactionalOperator;

    public TaskOrchestrator(
            TaskRepository taskRepo,
            TaskStepRepository stepRepo,
            TaskStateMachine stateMachine,
            TaskMessageProducer messageProducer,
            DatabaseClient databaseClient,
            TransactionalOperator transactionalOperator) {
        this.taskRepo = taskRepo;
        this.stepRepo = stepRepo;
        this.stateMachine = stateMachine;
        this.messageProducer = messageProducer;
        this.databaseClient = databaseClient;
        this.transactionalOperator = transactionalOperator;
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
                        log.debug("[TaskOrchestrator] 重复成功进度，跳过后续编排 taskId={} step={}",
                                taskId, stepName);
                        return Mono.just(task);
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
                                    return handleExtractStepSuccess(updatedTask, taskId);
                                }
                                return handleNonExtractStepSuccess(updatedTask, stepName, taskId);
                            });
                });
    }

    private Mono<Task> handleExtractStepSuccess(Task updatedTask, String taskId) {
        return checkAllExtractsDone(taskId)
                .flatMap(allDone -> {
                    if (Boolean.TRUE.equals(allDone)) {
                        updatedTask.setStatus(TaskStatus.EXTRACT_SUCCESS.name());
                        updatedTask.setProgress(PROGRESS_EXTRACT);
                        return taskRepo.save(updatedTask)
                                .flatMap(saved ->
                                        dispatchStep(taskId, TaskStepName.CHECK,
                                                buildPayload(saved)));
                    } else {
                        updatedTask.setStatus(TaskStatus.EXTRACT_PARTIAL.name());
                        return taskRepo.save(updatedTask);
                    }
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
                        return dispatchStep(taskId,
                                TaskStepName.valueOf(next),
                                buildPayload(saved));
                    }
                    return Mono.just(saved);
                });
    }

    /**
     * 将三个报表抽取步骤统一切换到运行态并逐个发布到 RabbitMQ。
     *
     * <p>任务级状态只从 {@code PARSE_SUCCESS} 转换一次到 {@code EXTRACT_RUNNING}；
     * 后续三个步骤不能复用 {@link #dispatchStep}，否则会触发不允许的
     * {@code EXTRACT_RUNNING -> EXTRACT_RUNNING} 自转换。</p>
     */
    private Mono<Task> dispatchExtractionSteps(Task task, Map<String, Object> payload) {
        TaskStatus current = TaskStatus.valueOf(task.getStatus());
        if (!stateMachine.canTransition(current, TaskStatus.EXTRACT_RUNNING)) {
            return Mono.error(new BusinessException(
                    org.springframework.http.HttpStatus.CONFLICT,
                    "INVALID_TRANSITION",
                    String.format("无法从 %s 转换到 %s", current, TaskStatus.EXTRACT_RUNNING)));
        }

        task.setStatus(TaskStatus.EXTRACT_RUNNING.name());
        task.setCurrentStep(TaskStepName.EXTRACT_BS.name());
        return taskRepo.save(task)
                .flatMap(saved -> Flux.fromIterable(EXTRACTION_STEPS)
                        .concatMap(step -> markExtractionStepRunningAndPublish(saved, step, payload))
                        .then(Mono.just(saved)));
    }

    /**
     * 将单个抽取步骤标记为 RUNNING 并发布对应的持久化消息。
     */
    private Mono<Void> markExtractionStepRunningAndPublish(
            Task task, TaskStepName step, Map<String, Object> payload) {
        return stepRepo.findByTaskIdAndStepName(task.getId(), step.name())
                .switchIfEmpty(Mono.error(new BusinessException(
                        org.springframework.http.HttpStatus.NOT_FOUND,
                        "TASK_STEP_NOT_FOUND", "任务步骤不存在: " + step.name())))
                .flatMap(stepRecord -> {
                    stepRecord.setStatus(StepStatus.RUNNING.name());
                    stepRecord.setStartedAt(LocalDateTime.now());
                    return stepRepo.save(stepRecord)
                            .then(Mono.deferContextual(context -> Mono.<Void>fromRunnable(() ->
                                    messageProducer.publishTaskStep(
                                            task.getId(),
                                            step.getRoutingKey(),
                                            payload,
                                            context.getOrDefault(TraceContext.TRACE_ID, "")))))
                            .onErrorResume(com.finreport.exception.IntegrationException.class,
                                    error -> markDispatchFailed(task, stepRecord, error)
                                            .then(Mono.<Void>error(error)));
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
        return stepRepo.findByTaskIdAndStepName(taskId, stepName)
                .switchIfEmpty(Mono.error(new BusinessException(
                        org.springframework.http.HttpStatus.NOT_FOUND,
                        "TASK_STEP_NOT_FOUND", "任务步骤不存在: " + stepName)))
                .flatMap(step -> scheduleRetryOrFail(task, step, result));
    }

    private Mono<Task> scheduleRetryOrFail(Task task, TaskStep step, Map<String, Object> result) {
        int retries = step.getRetryCount() == null ? 0 : step.getRetryCount();
        String taskId = task.getId();
        String stepName = step.getStepName();
        if (retries >= TaskStateMachine.MAX_RETRIES) {
            step.setStatus(StepStatus.FAILED.name());
            step.setFinishedAt(LocalDateTime.now());
            step.setErrorMsg(errorMessage(result));
            task.setStatus(TaskStatus.FAILED.name());
            task.setFinishedAt(LocalDateTime.now());
            task.setErrorMsg("步骤 " + stepName + " 重试耗尽");
            return stepRepo.save(step).then(taskRepo.save(task));
        }

        int nextRetry = retries + 1;
        TaskStatus retryStatus = stateMachine.decideRetryOrFail(
                stateMachine.onStepFailure(stepName), retries);
        step.setStatus(StepStatus.RETRY.name());
        step.setRetryCount(nextRetry);
        step.setErrorMsg(errorMessage(result));
        task.setStatus(retryStatus.name());
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
                .onErrorMap(com.finreport.exception.IntegrationException.class, error -> error);
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
