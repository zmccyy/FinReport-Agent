package com.finreport.domain.enums;

/**
 * 任务步骤名称枚举 — spec §3.2 定义的 6 个原子步骤。
 */
public enum TaskStepName {

    PARSE,

    EXTRACT_BS,
    EXTRACT_IS,
    EXTRACT_CF,

    CHECK,

    REPORT;

    /**
     * 是否为抽取类步骤（BS/IS/CF 三表）。
     */
    public boolean isExtract() {
        return this == EXTRACT_BS || this == EXTRACT_IS || this == EXTRACT_CF;
    }

    /**
     * 获取 MQ 路由键。
     */
    public String getRoutingKey() {
        return switch (this) {
            case PARSE -> "parse";
            case EXTRACT_BS -> "extract.bs";
            case EXTRACT_IS -> "extract.is";
            case EXTRACT_CF -> "extract.cf";
            case CHECK -> "check";
            case REPORT -> "report";
        };
    }
}
