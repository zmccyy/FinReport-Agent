package com.finreport.service.reasoner;

import java.time.ZoneOffset;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

import com.finreport.domain.dto.AccountingCheckResponse;
import com.finreport.domain.entity.AccountingCheck;
import com.finreport.domain.entity.Report;
import com.finreport.exception.BusinessException;
import com.finreport.repository.AccountingCheckRepository;
import com.finreport.repository.ReportRepository;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 勾稽结果查询服务 — spec §6.2.2 / M3.09。
 *
 * <p>按 reportId 查询 {@code accounting_check} 表，按 ruleType 排序；
 * 先做 report 归属校验（spec §8.5 用户隔离）。</p>
 */
@Service
public class CheckQueryService {

    private static final Logger log = LoggerFactory.getLogger(CheckQueryService.class);

    private final ReportRepository reportRepo;
    private final AccountingCheckRepository checkRepo;

    public CheckQueryService(ReportRepository reportRepo, AccountingCheckRepository checkRepo) {
        this.reportRepo = reportRepo;
        this.checkRepo = checkRepo;
    }

    /**
     * 查询某份报告的所有勾稽结果。
     *
     * @param reportId 报告 ID
     * @param userId   当前用户 ID
     * @return 勾稽结果列表 Flux；report 不存在或无权限抛 {@code REPORT_NOT_FOUND}
     */
    public Flux<AccountingCheckResponse> listChecks(Long reportId, Long userId) {
        log.debug("[CheckQueryService] listChecks reportId={} userId={}", reportId, userId);
        return assertReportOwnership(reportId, userId)
                .thenMany(checkRepo.findByReportIdOrderByRuleTypeAsc(reportId)
                        .map(CheckQueryService::toResponse));
    }

    private Mono<Report> assertReportOwnership(Long reportId, Long userId) {
        return reportRepo.findById(reportId)
                .filter(report -> userId.equals(report.getUserId()))
                .switchIfEmpty(Mono.error(new BusinessException(
                        HttpStatus.NOT_FOUND, "REPORT_NOT_FOUND",
                        "报告不存在: " + reportId)));
    }

    private static AccountingCheckResponse toResponse(AccountingCheck check) {
        return new AccountingCheckResponse(
                check.getId(),
                check.getRuleName(),
                check.getRuleType(),
                check.getExpected(),
                check.getActual(),
                check.getDiff(),
                check.getIsPass(),
                check.getSeverity(),
                check.getNote(),
                check.getCreatedAt() != null
                        ? check.getCreatedAt().atZone(ZoneOffset.UTC).toInstant()
                        : null);
    }
}
