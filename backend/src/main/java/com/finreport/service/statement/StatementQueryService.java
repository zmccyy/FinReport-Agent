package com.finreport.service.statement;

import java.util.List;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

import com.finreport.domain.dto.ReportDetailResponse;
import com.finreport.domain.dto.StatementItemResponse;
import com.finreport.domain.dto.StatementsResponse;
import com.finreport.domain.entity.FinancialStatementItem;
import com.finreport.domain.entity.Report;
import com.finreport.exception.BusinessException;
import com.finreport.repository.FinancialStatementRepository;
import com.finreport.repository.ReportRepository;

import reactor.core.publisher.Mono;

/**
 * 三表查询服务 — spec §6.2.2 / M2.11。
 *
 * <p>封装「按 reportId 查 report 元数据 + 三表分组查询」业务逻辑，
 * 让 {@code ReportController} 保持薄壳；同时校验 report 归属当前用户
 * （spec §8.5 安全：用户隔离）。</p>
 *
 * <p>三表类型常量 {@code balance_sheet} / {@code income_statement} / {@code cash_flow}
 * 与 L3 {@code StatementType.value} 锁定一致（spec §5.2.2）。</p>
 */
@Service
public class StatementQueryService {

    private static final Logger log = LoggerFactory.getLogger(StatementQueryService.class);

    public static final String TYPE_BALANCE_SHEET = "balance_sheet";
    public static final String TYPE_INCOME_STATEMENT = "income_statement";
    public static final String TYPE_CASH_FLOW = "cash_flow";

    private final ReportRepository reportRepo;
    private final FinancialStatementRepository fsRepo;

    public StatementQueryService(ReportRepository reportRepo, FinancialStatementRepository fsRepo) {
        this.reportRepo = reportRepo;
        this.fsRepo = fsRepo;
    }

    /**
     * 查询 report 详情（含归属校验）。
     *
     * @param reportId 报告 ID
     * @param userId   当前用户 ID（用于归属校验）
     * @return report 详情；不存在或不归属当前用户抛 {@code REPORT_NOT_FOUND}
     */
    public Mono<ReportDetailResponse> getReportDetail(Long reportId, Long userId) {
        log.debug("[StatementQueryService] getReportDetail reportId={} userId={}", reportId, userId);
        return reportRepo.findById(reportId)
                .filter(report -> report.getUserId().equals(userId))
                .switchIfEmpty(Mono.error(new BusinessException(
                        HttpStatus.NOT_FOUND, "REPORT_NOT_FOUND",
                        "报告不存在: " + reportId)))
                .map(StatementQueryService::toDetailResponse);
    }

    /**
     * 查询某份报告的三表数据（含归属校验）。
     *
     * <p>三表分别按 {@code statement_type} 过滤后组装为 {@link StatementsResponse}；
     * 三张表内按 {@code item_name} 字典序排序（{@code FinancialStatementRepository}
     * 的 derived query 已保证）。</p>
     *
     * @param reportId 报告 ID
     * @param userId   当前用户 ID
     * @return 三表分组数据；report 不存在或无权限抛 {@code REPORT_NOT_FOUND}
     */
    public Mono<StatementsResponse> getStatements(Long reportId, Long userId) {
        log.debug("[StatementQueryService] getStatements reportId={} userId={}", reportId, userId);
        return reportRepo.findById(reportId)
                .filter(report -> report.getUserId().equals(userId))
                .switchIfEmpty(Mono.error(new BusinessException(
                        HttpStatus.NOT_FOUND, "REPORT_NOT_FOUND",
                        "报告不存在: " + reportId)))
                .flatMap(report -> fsRepo.findByReportIdAndStatementType(reportId, TYPE_BALANCE_SHEET)
                        .collectList()
                        .flatMap(bs -> fsRepo.findByReportIdAndStatementType(reportId, TYPE_INCOME_STATEMENT)
                                .collectList()
                                .flatMap(is -> fsRepo.findByReportIdAndStatementType(reportId, TYPE_CASH_FLOW)
                                        .collectList()
                                        .map(cf -> new StatementsResponse(
                                                toResponses(bs),
                                                toResponses(is),
                                                toResponses(cf))))));
    }

    private static ReportDetailResponse toDetailResponse(Report report) {
        return new ReportDetailResponse(
                report.getId(),
                report.getTaskId(),
                report.getCompanyCode(),
                report.getCompanyName(),
                report.getReportType(),
                report.getReportPeriod(),
                report.getPageCount(),
                report.getParseStatus(),
                report.getPdfObjectKey(),
                report.getCreatedAt() != null ? report.getCreatedAt().atZone(java.time.ZoneOffset.UTC).toInstant()
                        : null
        );
    }

    private static List<StatementItemResponse> toResponses(List<FinancialStatementItem> items) {
        return items.stream()
                .map(StatementQueryService::toResponse)
                .toList();
    }

    private static StatementItemResponse toResponse(FinancialStatementItem item) {
        return new StatementItemResponse(
                item.getId(),
                item.getStatementType(),
                item.getItemName(),
                item.getItemValue(),
                item.getCurrency(),
                item.getUnit(),
                item.getScope(),
                item.getPeriodType(),
                item.getConfidence(),
                item.getSourcePage()
        );
    }
}
