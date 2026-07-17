package com.finreport.repository;

import org.springframework.data.repository.reactive.ReactiveCrudRepository;
import org.springframework.stereotype.Repository;

import com.finreport.domain.entity.Report;

import reactor.core.publisher.Mono;

/**
 * 财报元数据 Repository — 对应 report 表。
 */
@Repository
public interface ReportRepository extends ReactiveCrudRepository<Report, Long> {

    /**
     * 在所属用户范围内根据 pdf_md5 查找报告（用于隔离去重检测）。
     *
     * @param userId 用户 ID
     * @param pdfMd5 PDF 文件 MD5
     * @return 报告实体（可能为空）
     */
    Mono<Report> findByUserIdAndPdfMd5(Long userId, String pdfMd5);

    /**
     * Legacy unscoped lookup retained only for backwards-compatible repository callers.
     * Production upload paths must use {@link #findByUserIdAndPdfMd5(Long, String)}.
     */
    @Deprecated
    Mono<Report> findByPdfMd5(String pdfMd5);

    /**
     * 根据 task_id 查找报告。
     *
     * @param taskId 任务 ID
     * @return 报告实体（可能为空）
     */
    Mono<Report> findByTaskId(String taskId);
}
