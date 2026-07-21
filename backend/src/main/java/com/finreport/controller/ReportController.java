package com.finreport.controller;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.codec.multipart.FilePart;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;

import com.finreport.domain.dto.ReportDetailResponse;
import com.finreport.domain.dto.StatementsResponse;
import com.finreport.domain.dto.UploadResponse;
import com.finreport.exception.BusinessException;
import com.finreport.service.file.FileService;
import com.finreport.service.statement.StatementQueryService;

import reactor.core.publisher.Mono;

/**
 * 财报管理控制器 — spec §6.2.2。
 *
 * <p>提供财报上传 + 详情 + 三表查询端点。
 * 所有端点通过 JwtFilter 验证 Bearer Token，{@code X-User-Id} 头由
 * {@code JwtFilter} 注入。</p>
 */
@RestController
@RequestMapping("/api/v1")
public class ReportController {

    private static final Logger log = LoggerFactory.getLogger(ReportController.class);

    private final FileService fileService;
    private final StatementQueryService statementQueryService;

    @Autowired
    public ReportController(FileService fileService, StatementQueryService statementQueryService) {
        this.fileService = fileService;
        this.statementQueryService = statementQueryService;
    }

    /** Backward-compatible constructor for isolated upload tests. */
    public ReportController(FileService fileService) {
        this(fileService, null);
    }

    /**
     * 上传 PDF 财报 — spec §6.3.1。
     *
     * <p>{@code POST /api/v1/reports/upload}
     * Content-Type: multipart/form-data。支持 Idempotency-Key 去重。</p>
     *
     * @param filePart       上传的 PDF 文件（part name: "file"）
     * @param companyCode    公司代码（如 600519）
     * @param companyName    公司名称（如 贵州茅台）
     * @param reportType     报告类型（如 ANNUAL）
     * @param reportPeriod   报告期间（如 2024-12-31）
     * @param userIdHeader   X-User-Id Header（由 JwtFilter 注入）
     * @param idempotencyKey Idempotency-Key Header（可选）
     * @return UploadResponse（HTTP 201）或错误（RFC 9457）
     */
    @PostMapping(value = "/reports/upload", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public Mono<ResponseEntity<UploadResponse>> upload(
            @RequestPart("file") Mono<FilePart> filePartMono,
            @RequestPart("companyCode") String companyCode,
            @RequestPart("companyName") String companyName,
            @RequestPart("reportType") String reportType,
            @RequestPart("reportPeriod") String reportPeriod,
            @RequestHeader("X-User-Id") Long userIdHeader,
            @RequestHeader(value = "Idempotency-Key", required = false) String idempotencyKey) {

        log.debug("[ReportController] POST /reports/upload userId={} companyCode={}",
                userIdHeader, companyCode);

        return filePartMono.flatMap(filePart ->
                fileService.upload(
                        filePart, companyCode, companyName,
                        reportType, reportPeriod, userIdHeader, idempotencyKey)
        ).map(response -> {
            log.info("[ReportController] 上传成功 taskId={} reportId={}",
                    response.taskId(), response.reportId());
            return ResponseEntity.status(HttpStatus.CREATED).body(response);
        }).onErrorMap(e -> {
            if (!(e instanceof BusinessException)) {
                log.error("[ReportController] 上传失败 userId={} companyCode={}",
                        userIdHeader, companyCode, e);
                return new BusinessException(
                        HttpStatus.INTERNAL_SERVER_ERROR,
                        "UPLOAD_FAILED",
                        "文件上传失败: " + e.getMessage());
            }
            return e;
        });
    }

    /**
     * 查询财报详情 — spec §6.2.2 / M2.11。
     *
     * <p>{@code GET /api/v1/reports/{reportId}}。
     * 用于详情页头部展示公司信息、报告期间、PDF 元数据。</p>
     *
     * @param reportId    报告 ID
     * @param userIdHeader X-User-Id（归属校验）
     * @return 报告详情；不存在或不归属当前用户返回 404
     */
    @GetMapping("/reports/{reportId}")
    public Mono<ResponseEntity<ReportDetailResponse>> getReport(
            @PathVariable("reportId") Long reportId,
            @RequestHeader("X-User-Id") Long userIdHeader) {
        log.debug("[ReportController] GET /reports/{} userId={}", reportId, userIdHeader);
        return statementQueryService.getReportDetail(reportId, userIdHeader)
                .map(ResponseEntity::ok);
    }

    /**
     * 查询三表数据 — spec §6.2.2 / M2.11。
     *
     * <p>{@code GET /api/v1/reports/{reportId}/statements}。
     * 一次性返回 BS/IS/CF 三张报表的科目条目，前端按 Tab 切换渲染。</p>
     *
     * @param reportId    报告 ID
     * @param userIdHeader X-User-Id（归属校验）
     * @return 三表分组数据；report 不存在或无权限返回 404
     */
    @GetMapping("/reports/{reportId}/statements")
    public Mono<ResponseEntity<StatementsResponse>> getStatements(
            @PathVariable("reportId") Long reportId,
            @RequestHeader("X-User-Id") Long userIdHeader) {
        log.debug("[ReportController] GET /reports/{}/statements userId={}", reportId, userIdHeader);
        return statementQueryService.getStatements(reportId, userIdHeader)
                .map(ResponseEntity::ok);
    }
}
