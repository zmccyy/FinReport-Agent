package com.finreport.domain.enums;

/**
 * 任务状态枚举 — spec §3.2.1 完整状态定义。
 *
 * <p>状态值会写入 MySQL task.status 字段，请勿随意变更枚举名。</p>
 */
public enum TaskStatus {

    // --- 初始 ---
    PENDING,

    // --- 解析阶段 ---
    PARSE_RUNNING,
    PARSE_SUCCESS,
    PARSE_FAILED,
    PARSE_RETRY,

    // --- 抽取阶段（三表并行）---
    EXTRACT_RUNNING,
    EXTRACT_PARTIAL,
    EXTRACT_SUCCESS,
    EXTRACT_FAILED,
    EXTRACT_RETRY,

    // --- 勾稽阶段 ---
    CHECK_RUNNING,
    CHECK_SUCCESS,
    CHECK_FAILED,
    CHECK_RETRY,

    // --- 报告阶段 ---
    REPORT_RUNNING,
    REPORT_SUCCESS,
    REPORT_FAILED,
    REPORT_RETRY,

    // --- 终态 ---
    COMPLETED,
    FAILED,
    CANCELLED;

    /**
     * 是否为终态（不可再变更）。
     */
    public boolean isTerminal() {
        return this == COMPLETED || this == FAILED || this == CANCELLED;
    }

    /**
     * 是否为运行态。
     */
    public boolean isRunning() {
        return this.name().endsWith("_RUNNING");
    }
}
