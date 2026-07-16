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
 * 财报元数据实体，映射 report 表 — V2__init_report.sql。
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Table("report")
public class Report {

    @Id
    private Long id;

    @Column("task_id")
    private String taskId;

    @Column("user_id")
    private Long userId;

    @Column("company_code")
    private String companyCode;

    @Column("company_name")
    private String companyName;

    @Column("report_type")
    private String reportType;

    @Column("report_period")
    private String reportPeriod;

    @Column("pdf_md5")
    private String pdfMd5;

    @Column("pdf_object_key")
    private String pdfObjectKey;

    @Column("page_count")
    private Integer pageCount;

    @Column("parse_status")
    private String parseStatus;

    @Column("created_at")
    private LocalDateTime createdAt;
}
