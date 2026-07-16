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
 * 任务编排实体，映射 task 表 — V3__init_task.sql。
 *
 * <p>注意：不实现 Persistable。使用手动 ID（task-xxx），新建时通过
 * DatabaseClient 显式 INSERT；更新时通过 Repository.save() 执行 UPDATE。</p>
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Table("task")
public class Task {

    @Id
    private String id;

    @Column("user_id")
    private Long userId;

    @Column("task_type")
    private String taskType;

    @Column("ref_report_id")
    private Long refReportId;

    private String status;

    @Column("current_step")
    private String currentStep;

    private Integer progress;

    /** JSON 格式 — 任务参数 */
    private String payload;

    /** JSON 格式 — 任务结果 */
    private String result;

    @Column("error_msg")
    private String errorMsg;

    @Column("started_at")
    private LocalDateTime startedAt;

    @Column("finished_at")
    private LocalDateTime finishedAt;

    @Column("created_at")
    private LocalDateTime createdAt;
}
