package com.finreport.domain.entity;

import java.time.LocalDateTime;

import org.springframework.data.annotation.Id;
import org.springframework.data.relational.core.mapping.Column;
import org.springframework.data.relational.core.mapping.Table;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 任务步骤实体，映射 task_step 表 — V3__init_task.sql。
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Table("task_step")
public class TaskStep {

    @Id
    private Long id;

    @Column("task_id")
    private String taskId;

    @Column("step_name")
    private String stepName;

    private String status;

    @Column("started_at")
    private LocalDateTime startedAt;

    @Column("finished_at")
    private LocalDateTime finishedAt;

    @Column("duration_ms")
    private Integer durationMs;

    @Column("message_id")
    private String messageId;

    @Column("error_msg")
    private String errorMsg;
}
