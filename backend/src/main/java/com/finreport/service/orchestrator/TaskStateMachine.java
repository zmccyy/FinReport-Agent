package com.finreport.service.orchestrator;

import java.util.EnumMap;
import java.util.Map;
import java.util.Set;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import com.finreport.domain.enums.TaskStatus;

/**
 * 任务状态机 — spec §3.2.1 状态转换规则。
 *
 * <p>线程安全，纯函数，不依赖外部状态。
 * 所有转换规则集中在 {@link #ALLOWED} 映射表中。</p>
 */
@Component
public class TaskStateMachine {

    private static final Logger log = LoggerFactory.getLogger(TaskStateMachine.class);

    /**
     * 最大重试次数。
     *
     * 每个步骤最多重新投递三次；第 3 次重试仍失败后任务进入 FAILED。
     */
    public static final int MAX_RETRIES = 3;

    /** 从每个状态允许的下一状态集合 */
    private static final Map<TaskStatus, Set<TaskStatus>> ALLOWED = new EnumMap<>(TaskStatus.class);

    static {
        // 初始
        ALLOWED.put(TaskStatus.PENDING, Set.of(TaskStatus.PARSE_RUNNING, TaskStatus.CANCELLED));

        // 解析阶段
        // M2 review fix: 加 FAILED — MQ dispatch 投递失败时 markDispatchFailed 直接转终态 FAILED,
        // 这是 dispatch 失败的快速终态路径(非 step 执行失败,不走 *_FAILED → retry)。
        ALLOWED.put(TaskStatus.PARSE_RUNNING,
                Set.of(TaskStatus.PARSE_SUCCESS, TaskStatus.PARSE_FAILED,
                        TaskStatus.FAILED, TaskStatus.CANCELLED));
        ALLOWED.put(TaskStatus.PARSE_SUCCESS, Set.of(TaskStatus.EXTRACT_RUNNING));
        ALLOWED.put(TaskStatus.PARSE_FAILED,
                Set.of(TaskStatus.PARSE_RETRY, TaskStatus.FAILED));
        ALLOWED.put(TaskStatus.PARSE_RETRY,
                Set.of(TaskStatus.PARSE_RUNNING, TaskStatus.PARSE_SUCCESS, TaskStatus.PARSE_FAILED));

        // 抽取阶段
        ALLOWED.put(TaskStatus.EXTRACT_RUNNING,
                Set.of(TaskStatus.EXTRACT_PARTIAL, TaskStatus.EXTRACT_SUCCESS,
                        TaskStatus.EXTRACT_FAILED, TaskStatus.FAILED, TaskStatus.CANCELLED));
        ALLOWED.put(TaskStatus.EXTRACT_PARTIAL,
                Set.of(TaskStatus.EXTRACT_SUCCESS, TaskStatus.EXTRACT_FAILED,
                        TaskStatus.FAILED, TaskStatus.CANCELLED));
        ALLOWED.put(TaskStatus.EXTRACT_SUCCESS, Set.of(TaskStatus.CHECK_RUNNING));
        ALLOWED.put(TaskStatus.EXTRACT_FAILED,
                Set.of(TaskStatus.EXTRACT_RETRY, TaskStatus.FAILED));
        ALLOWED.put(TaskStatus.EXTRACT_RETRY,
                Set.of(TaskStatus.EXTRACT_RUNNING, TaskStatus.EXTRACT_SUCCESS,
                        TaskStatus.EXTRACT_PARTIAL, TaskStatus.EXTRACT_FAILED));

        // 勾稽阶段
        ALLOWED.put(TaskStatus.CHECK_RUNNING,
                Set.of(TaskStatus.CHECK_SUCCESS, TaskStatus.CHECK_FAILED,
                        TaskStatus.FAILED, TaskStatus.CANCELLED));
        ALLOWED.put(TaskStatus.CHECK_SUCCESS, Set.of(TaskStatus.REPORT_RUNNING));
        ALLOWED.put(TaskStatus.CHECK_FAILED,
                Set.of(TaskStatus.CHECK_RETRY, TaskStatus.FAILED));
        ALLOWED.put(TaskStatus.CHECK_RETRY,
                Set.of(TaskStatus.CHECK_RUNNING, TaskStatus.CHECK_SUCCESS, TaskStatus.CHECK_FAILED));

        // 报告阶段
        ALLOWED.put(TaskStatus.REPORT_RUNNING,
                Set.of(TaskStatus.REPORT_SUCCESS, TaskStatus.REPORT_FAILED,
                        TaskStatus.FAILED, TaskStatus.CANCELLED));
        ALLOWED.put(TaskStatus.REPORT_SUCCESS, Set.of(TaskStatus.COMPLETED));
        ALLOWED.put(TaskStatus.REPORT_FAILED,
                Set.of(TaskStatus.REPORT_RETRY, TaskStatus.FAILED));
        ALLOWED.put(TaskStatus.REPORT_RETRY,
                Set.of(TaskStatus.REPORT_RUNNING, TaskStatus.REPORT_SUCCESS, TaskStatus.REPORT_FAILED));

        // 终态
        ALLOWED.put(TaskStatus.COMPLETED, Set.of());
        ALLOWED.put(TaskStatus.FAILED, Set.of());
        ALLOWED.put(TaskStatus.CANCELLED, Set.of());
    }

    /**
     * 校验状态转换是否合法。
     *
     * @param from 当前状态
     * @param to   目标状态
     * @return true 如果转换合法
     */
    public boolean canTransition(TaskStatus from, TaskStatus to) {
        Set<TaskStatus> allowed = ALLOWED.get(from);
        return allowed != null && allowed.contains(to);
    }

    /**
     * 获取当前状态允许的下一状态集合。
     *
     * @param current 当前状态
     * @return 允许的下一状态集合（终态返回空集）
     */
    public Set<TaskStatus> allowedNext(TaskStatus current) {
        return ALLOWED.getOrDefault(current, Set.of());
    }

    /**
     * 确认状态字符串是否为有效的 TaskStatus 枚举值。
     *
     * @param statusName 状态名称（可为 null）
     * @return true 如果状态有效
     */
    public boolean isValid(String statusName) {
        if (statusName == null) {
            return false;
        }
        try {
            TaskStatus.valueOf(statusName);
            return true;
        } catch (IllegalArgumentException e) {
            return false;
        }
    }

    /**
     * 解析状态字符串为枚举。
     */
    public TaskStatus parse(String statusName) {
        return TaskStatus.valueOf(statusName);
    }

    /**
     * 判断是否应进入重试状态。
     *
     * @param currentStatus 当前失败状态（PARSE_FAILED / EXTRACT_FAILED / CHECK_FAILED / REPORT_FAILED）
     * @param retryCount    已重试次数
     * @return 对应 RETRY 状态，或 FAILED（重试耗尽）
     */
    public TaskStatus decideRetryOrFail(TaskStatus currentStatus, int retryCount) {
        if (retryCount < MAX_RETRIES) {
            return switch (currentStatus) {
                case PARSE_FAILED -> TaskStatus.PARSE_RETRY;
                case EXTRACT_FAILED -> TaskStatus.EXTRACT_RETRY;
                case CHECK_FAILED -> TaskStatus.CHECK_RETRY;
                case REPORT_FAILED -> TaskStatus.REPORT_RETRY;
                default -> {
                    log.warn("[TaskStateMachine] 非失败状态调用了 decideRetryOrFail status={}", currentStatus);
                    yield TaskStatus.FAILED;
                }
            };
        }
        return TaskStatus.FAILED;
    }

    /**
     * 步骤成功后应进入哪个任务级别状态。
     *
     * @param stepSuccess 成功的步骤所属阶段
     * @return 对应 _SUCCESS 状态
     */
    public TaskStatus onStepSuccess(String stepName) {
        return switch (stepName) {
            case "PARSE" -> TaskStatus.PARSE_SUCCESS;
            case "CHECK" -> TaskStatus.CHECK_SUCCESS;
            case "REPORT" -> TaskStatus.REPORT_SUCCESS;
            default -> {
                if (stepName.startsWith("EXTRACT_")) {
                    yield TaskStatus.EXTRACT_SUCCESS;
                }
                log.warn("[TaskStateMachine] 未知步骤名 step={}", stepName);
                yield TaskStatus.FAILED;
            }
        };
    }

    /**
     * 步骤失败后应进入哪个任务级别状态。
     *
     * @param stepFailed 失败的步骤所属阶段
     * @return 对应 _FAILED 状态
     */
    public TaskStatus onStepFailure(String stepName) {
        return switch (stepName) {
            case "PARSE" -> TaskStatus.PARSE_FAILED;
            case "CHECK" -> TaskStatus.CHECK_FAILED;
            case "REPORT" -> TaskStatus.REPORT_FAILED;
            default -> {
                if (stepName.startsWith("EXTRACT_")) {
                    yield TaskStatus.EXTRACT_FAILED;
                }
                log.warn("[TaskStateMachine] 未知步骤名 step={}", stepName);
                yield TaskStatus.FAILED;
            }
        };
    }

    /**
     * 根据阶段名称获取 RUNNING 状态。
     */
    public TaskStatus runningStateFor(String stepName) {
        return switch (stepName) {
            case "PARSE" -> TaskStatus.PARSE_RUNNING;
            case "CHECK" -> TaskStatus.CHECK_RUNNING;
            case "REPORT" -> TaskStatus.REPORT_RUNNING;
            default -> {
                if (stepName.startsWith("EXTRACT_")) {
                    yield TaskStatus.EXTRACT_RUNNING;
                }
                yield TaskStatus.PARSE_RUNNING;
            }
        };
    }

    /**
     * 获取下一阶段的首个步骤名。
     */
    public String nextStepAfter(String currentStep) {
        return switch (currentStep) {
            case "PARSE" -> "EXTRACT_BS";
            case "EXTRACT_BS", "EXTRACT_IS", "EXTRACT_CF", "EXTRACT_COMPLETE" -> "CHECK";
            case "CHECK" -> "REPORT";
            case "REPORT" -> null;
            default -> null;
        };
    }
}
