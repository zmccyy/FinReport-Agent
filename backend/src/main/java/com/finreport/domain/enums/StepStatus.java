package com.finreport.domain.enums;

/**
 * 步骤状态枚举。
 *
 * <p>每个 task_step 有自己的独立状态，对外通过 TaskOrchestrator
 * 聚合为任务级别的状态。</p>
 */
public enum StepStatus {

    PENDING,
    RUNNING,
    SUCCESS,
    FAILED,
    RETRY;
}
