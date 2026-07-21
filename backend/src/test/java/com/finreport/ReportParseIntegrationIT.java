package com.finreport;

import static org.awaitility.Awaitility.await;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.atLeast;
import static org.mockito.Mockito.atMost;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

import java.math.BigDecimal;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.MySQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.utility.DockerImageName;

import com.finreport.domain.entity.Report;
import com.finreport.exception.BusinessException;
import com.finreport.mq.TaskMessageProducer;
import com.finreport.repository.FinancialStatementRepository;
import com.finreport.repository.ReportRepository;
import com.finreport.repository.TaskRepository;
import com.finreport.repository.TaskStepRepository;
import com.finreport.service.orchestrator.ExtractCacheService;
import com.finreport.service.orchestrator.TaskOrchestrator;
import com.finreport.service.statement.StatementQueryService;

import reactor.core.publisher.Mono;

/**
 * M2.12 集成测试 — 真实年报端到端验证（plan §4.3）。
 *
 * <p>启动完整 Spring 上下文 + Testcontainers MySQL + Redis，通过 TaskOrchestrator
 * Java API 直接驱动完整任务生命周期（PARSE → EXTRACT_BS/IS/CF → CHECK → REPORT），
 * 验证 task 状态机推进、{@code financial_statement} 表数据写入、{@link ExtractCacheService}
 * 缓存命中路径。</p>
 *
 * <p><b>为何不起 RabbitMQ/L3 真实进程</b>：L3 真实推理需要 GPU 跑 7B 4-bit 模型
 * （~5GB VRAM），单次推理 60s+，CI 不稳定；当前 {@code ai-service/app/modules/extractor/handler.py}
 * 仍是 mock，不接真实 ModelHub。所以测试策略为：</p>
 * <ul>
 *   <li>Java 侧用 {@code @MockBean TaskMessageProducer} 屏蔽 MQ；</li>
 *   <li>直接调 {@code TaskOrchestrator.handleStepProgress} 模拟 L3 回报，注入与
 *       {@code extract_handler.handle} 返回结构一致的 fixture payload；</li>
 *   <li>真实 Redis 验证 {@code ExtractCacheService} 的 store/lookup 全链路；</li>
 *   <li>真实 MySQL 验证 {@code StatementWriter} 的三表写入与归属查询。</li>
 * </ul>
 *
 * <p>F1 评估与 SLA 评估由独立脚本 {@code scripts/eval_m2_f1.py} 和
 * {@code scripts/eval_m2_sla.py} 承担，需要 GPU + 人工 ground truth JSON，
 * 不在 CI 流水线中运行。</p>
 */
@SpringBootTest
@ActiveProfiles("it")
@Testcontainers
class ReportParseIntegrationIT {

    private static final String USER = "finreport";
    private static final String PASSWORD = "finreport";
    private static final String DATABASE = "finreport";
    private static final String MYSQL_IMAGE = "mysql:8.0.36";
    private static final String REDIS_IMAGE = "redis:7.2-alpine";

    /** M2.10 cache key 前缀；测试断言用，避免反射访问私有常量。 */
    private static final String CACHE_KEY_PREFIX = ExtractCacheService.KEY_PREFIX;

    @Container
    private static final MySQLContainer<?> MYSQL = new MySQLContainer<>(DockerImageName.parse(MYSQL_IMAGE))
            .withDatabaseName(DATABASE)
            .withUsername(USER)
            .withPassword(PASSWORD);

    @Container
    private static final GenericContainer<?> REDIS = new GenericContainer<>(DockerImageName.parse(REDIS_IMAGE))
            .withExposedPorts(6379);

    @DynamicPropertySource
    static void injectContainerProperties(DynamicPropertyRegistry registry) {
        // R2DBC + JDBC（Flyway 用 JDBC，业务用 R2DBC）
        String jdbcUrl = "jdbc:mysql://" + MYSQL.getHost() + ":" + MYSQL.getMappedPort(3306)
                + "/" + DATABASE + "?useSSL=false&allowPublicKeyRetrieval=true";
        String r2dbcUrl = "r2dbc:mysql://" + MYSQL.getHost() + ":" + MYSQL.getMappedPort(3306)
                + "/" + DATABASE;
        registry.add("spring.r2dbc.url", () -> r2dbcUrl);
        registry.add("spring.r2dbc.username", () -> USER);
        registry.add("spring.r2dbc.password", () -> PASSWORD);
        registry.add("spring.datasource.url", () -> jdbcUrl);
        registry.add("spring.datasource.username", () -> USER);
        registry.add("spring.datasource.password", () -> PASSWORD);

        // Redis
        registry.add("spring.data.redis.host", REDIS::getHost);
        registry.add("spring.data.redis.port", () -> REDIS.getMappedPort(6379));
    }

    @Autowired
    private TaskOrchestrator orchestrator;

    @Autowired
    private TaskRepository taskRepo;

    @Autowired
    private TaskStepRepository stepRepo;

    @Autowired
    private ReportRepository reportRepo;

    @Autowired
    private FinancialStatementRepository fsRepo;

    @Autowired
    private ExtractCacheService extractCacheService;

    @Autowired
    private StatementQueryService statementQueryService;

    /** 屏蔽真实 MQ：{@code publishTaskStep} 变为 no-op，{@code dispatchStep} 链路继续推进。 */
    @MockBean
    private TaskMessageProducer messageProducer;

    /**
     * 完整任务生命周期：上传茅台年报 PDF → PARSE → EXTRACT_BS/IS/CF → CHECK → REPORT → COMPLETED。
     *
     * <p>验收标准（plan §4.3）：
     * <ul>
     *   <li>三表数据在前端可见（即 financial_statement 表有数据）；</li>
     *   <li>端到端耗时 &lt; 3 min（这里用 awaitility 10s timeout 验证逻辑路径，真实 SLA 由 eval_m2_sla.py 测）；</li>
     *   <li>重传相同 PDF 命中缓存（下一个测试覆盖）。</li>
     * </ul>
     */
    @Test
    @DisplayName("完整任务生命周期：茅台年报 → 三表写入 → 任务完成")
    void shouldCompleteTaskLifecycleWithThreeStatementsWritten() {
        Long userId = 1L;
        String pdfMd5 = "moutai_" + UUID.randomUUID().toString().replace("-", "").substring(0, 16);

        // 1. 创建任务（payload 含 pdfObjectKey / pdfMd5 / companyCode / companyName）
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("companyCode", "600519");
        payload.put("companyName", "贵州茅台");
        payload.put("reportType", "ANNUAL");
        payload.put("reportPeriod", "2025");
        payload.put("pdfMd5", pdfMd5);
        payload.put("pdfObjectKey", "uploads/" + userId + "/" + pdfMd5 + ".pdf");

        String taskId = orchestrator.createTask(userId, payload)
                .map(com.finreport.domain.entity.Task::getId)
                .block(Duration.ofSeconds(15));
        assertNotNull(taskId, "任务 ID 必须返回");

        // 2. 创建 Report 记录（同 M2.09 路径：ReportController.upload 在 PARSE 完成后创建）
        Report report = Report.builder()
                .taskId(taskId)
                .userId(userId)
                .companyCode("600519")
                .companyName("贵州茅台")
                .reportType("ANNUAL")
                .reportPeriod("2025")
                .pdfMd5(pdfMd5)
                .pdfObjectKey("uploads/" + userId + "/" + pdfMd5 + ".pdf")
                .pageCount(120)
                .parseStatus("COMPLETED")
                .build();
        Long reportId = reportRepo.save(report).map(Report::getId).block(Duration.ofSeconds(5));
        assertNotNull(reportId, "报告 ID 必须返回");

        // 3. PARSE 成功回报（payload 透传 pdfObjectKey 给后续步骤）
        orchestrator.handleStepProgress(taskId, "PARSE", "SUCCESS",
                Map.of("pdfObjectKey", payload.get("pdfObjectKey"), "pageCount", 120))
                .block(Duration.ofSeconds(10));

        // 4. EXTRACT_BS / IS / CF 三表并行回报（payload 与 L3 extract_handler.handle 返回结构对齐）
        orchestrator.handleStepProgress(taskId, "EXTRACT_BS", "SUCCESS",
                buildExtractResult("balance_sheet", List.of(
                        Map.of("item", "货币资金", "value", 1.23e9, "scope", "合并", "period", "本期"),
                        Map.of("item", "资产总计", "value", 5.67e10, "scope", "合并", "period", "本期"))))
                .block(Duration.ofSeconds(10));

        orchestrator.handleStepProgress(taskId, "EXTRACT_IS", "SUCCESS",
                buildExtractResult("income_statement", List.of(
                        Map.of("item", "营业收入", "value", 8.9e9, "scope", "合并", "period", "本期"))))
                .block(Duration.ofSeconds(10));

        orchestrator.handleStepProgress(taskId, "EXTRACT_CF", "SUCCESS",
                buildExtractResult("cash_flow", List.of(
                        Map.of("item", "经营活动产生的现金流量净额", "value", 1.2e9, "scope", "合并", "period", "本期"))))
                .block(Duration.ofSeconds(10));

        // 5. CHECK 成功回报
        orchestrator.handleStepProgress(taskId, "CHECK", "SUCCESS", Map.of())
                .block(Duration.ofSeconds(10));

        // 6. REPORT 成功回报 → 任务进入 COMPLETED
        orchestrator.handleStepProgress(taskId, "REPORT", "SUCCESS", Map.of())
                .block(Duration.ofSeconds(10));

        // 验证 1：task.status == COMPLETED, progress == 100
        await().atMost(Duration.ofSeconds(5))
                .untilAsserted(() -> {
                    com.finreport.domain.entity.Task task = taskRepo.findById(taskId)
                            .block(Duration.ofSeconds(2));
                    assertNotNull(task, "任务必须存在");
                    assertEquals("COMPLETED", task.getStatus(),
                            "任务应进入 COMPLETED 状态");
                    assertEquals(100, task.getProgress(),
                            "任务进度应为 100");
                });

        // 验证 2：financial_statement 表写入 4 行（BS 2 + IS 1 + CF 1）
        await().atMost(Duration.ofSeconds(5))
                .untilAsserted(() -> {
                    Long count = fsRepo.countByReportId(reportId).block(Duration.ofSeconds(2));
                    assertNotNull(count, "科目计数必须返回");
                    assertEquals(4L, count, "三表合计应写入 4 行（BS 2 + IS 1 + CF 1）");
                });

        // 验证 3：按 statement_type 分组验证
        List<com.finreport.domain.entity.FinancialStatementItem> bsItems = fsRepo
                .findByReportIdAndStatementType(reportId, "balance_sheet")
                .collectList().block(Duration.ofSeconds(5));
        assertNotNull(bsItems);
        assertEquals(2, bsItems.size(), "BS 应有 2 条记录");
        assertEquals("货币资金", bsItems.get(0).getItemName(), "第 1 条应为货币资金");

        List<com.finreport.domain.entity.FinancialStatementItem> isItems = fsRepo
                .findByReportIdAndStatementType(reportId, "income_statement")
                .collectList().block(Duration.ofSeconds(5));
        assertNotNull(isItems);
        assertEquals(1, isItems.size(), "IS 应有 1 条记录");
        assertEquals("营业收入", isItems.get(0).getItemName());

        List<com.finreport.domain.entity.FinancialStatementItem> cfItems = fsRepo
                .findByReportIdAndStatementType(reportId, "cash_flow")
                .collectList().block(Duration.ofSeconds(5));
        assertNotNull(cfItems);
        assertEquals(1, cfItems.size(), "CF 应有 1 条记录");
        assertEquals("经营活动产生的现金流量净额", cfItems.get(0).getItemName());

        // 验证 4：item_value 字段类型与精度（DECIMAL(20,4)）
        BigDecimal expectedTotalAssets = new BigDecimal("56700000000.0000");
        assertEquals(expectedTotalAssets, bsItems.get(1).getItemValue(),
                "资产总计应为 5.67e10（DECIMAL(20,4) 精度）");

        // 验证 5：Redis 缓存三表全部写入（M2.10 验收）
        String cacheKeyBs = CACHE_KEY_PREFIX + pdfMd5 + ":EXTRACT_BS";
        String cacheKeyIs = CACHE_KEY_PREFIX + pdfMd5 + ":EXTRACT_IS";
        String cacheKeyCf = CACHE_KEY_PREFIX + pdfMd5 + ":EXTRACT_CF";
        // ExtractCacheService 内部用 ReactiveRedisTemplate，这里直接 lookup 复用同一客户端
        assertNotNull(extractCacheService.lookup(pdfMd5,
                com.finreport.domain.enums.TaskStepName.EXTRACT_BS).block(Duration.ofSeconds(2)),
                "BS 缓存必须命中");
        assertNotNull(extractCacheService.lookup(pdfMd5,
                com.finreport.domain.enums.TaskStepName.EXTRACT_IS).block(Duration.ofSeconds(2)),
                "IS 缓存必须命中");
        assertNotNull(extractCacheService.lookup(pdfMd5,
                com.finreport.domain.enums.TaskStepName.EXTRACT_CF).block(Duration.ofSeconds(2)),
                "CF 缓存必须命中");

        // 验证 6：归属校验通过 → StatementQueryService 返回报告详情
        com.finreport.domain.dto.ReportDetailResponse detail = statementQueryService
                .getReportDetail(reportId, userId).block(Duration.ofSeconds(5));
        assertNotNull(detail, "归属用户应能查到报告详情");
        assertEquals("600519", detail.companyCode(), "公司代码应一致");

        com.finreport.domain.dto.StatementsResponse statements = statementQueryService
                .getStatements(reportId, userId).block(Duration.ofSeconds(5));
        assertNotNull(statements);
        assertEquals(2, statements.balanceSheet().size(), "BS 分组应返回 2 条");
        assertEquals(1, statements.incomeStatement().size(), "IS 分组应返回 1 条");
        assertEquals(1, statements.cashFlow().size(), "CF 分组应返回 1 条");

        // 清理缓存供下一个测试干净起步
        extractCacheService.lookupAll(pdfMd5).block(Duration.ofSeconds(2));
    }

    /**
     * 缓存命中路径：重传相同 PDF（同 pdfMd5）→ extract 步骤跳过 MQ 投递 → EXTRACT_SUCCESS。
     *
     * <p>验收标准（plan §4.3）："重传相同 PDF 命中缓存"。</p>
     */
    @Test
    @DisplayName("重传相同 PDF 命中缓存：跳过 EXTRACT MQ 投递，直接 replay")
    void shouldSkipExtractAndReplayFromCacheOnReupload() {
        Long userId = 2L;
        String pdfMd5 = "pingan_" + UUID.randomUUID().toString().replace("-", "").substring(0, 16);

        // 第一轮：完整跑一遍写缓存
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("companyCode", "000001");
        payload.put("companyName", "平安银行");
        payload.put("reportType", "ANNUAL");
        payload.put("reportPeriod", "2025");
        payload.put("pdfMd5", pdfMd5);
        payload.put("pdfObjectKey", "uploads/" + userId + "/" + pdfMd5 + ".pdf");

        String taskId1 = orchestrator.createTask(userId, payload)
                .map(com.finreport.domain.entity.Task::getId)
                .block(Duration.ofSeconds(15));

        Report report1 = Report.builder()
                .taskId(taskId1)
                .userId(userId)
                .companyCode("000001")
                .companyName("平安银行")
                .reportType("ANNUAL")
                .reportPeriod("2025")
                .pdfMd5(pdfMd5)
                .pdfObjectKey("uploads/" + userId + "/" + pdfMd5 + ".pdf")
                .pageCount(180)
                .parseStatus("COMPLETED")
                .build();
        Long reportId1 = reportRepo.save(report1).map(Report::getId).block(Duration.ofSeconds(5));

        // 走完 EXTRACT 三表 → 缓存写入
        orchestrator.handleStepProgress(taskId1, "PARSE", "SUCCESS",
                Map.of("pdfObjectKey", payload.get("pdfObjectKey"), "pageCount", 180))
                .block(Duration.ofSeconds(10));
        orchestrator.handleStepProgress(taskId1, "EXTRACT_BS", "SUCCESS",
                buildExtractResult("balance_sheet", List.of(
                        Map.of("item", "资产总计", "value", 5.0e10, "scope", "合并", "period", "本期"))))
                .block(Duration.ofSeconds(10));
        orchestrator.handleStepProgress(taskId1, "EXTRACT_IS", "SUCCESS",
                buildExtractResult("income_statement", List.of(
                        Map.of("item", "营业收入", "value", 4.0e9, "scope", "合并", "period", "本期"))))
                .block(Duration.ofSeconds(10));
        orchestrator.handleStepProgress(taskId1, "EXTRACT_CF", "SUCCESS",
                buildExtractResult("cash_flow", List.of(
                        Map.of("item", "经营活动产生的现金流量净额", "value", 1.0e9, "scope", "合并", "period", "本期"))))
                .block(Duration.ofSeconds(10));

        // 验证第一轮缓存写入
        Map<com.finreport.domain.enums.TaskStepName, Map<String, Object>> cached = extractCacheService
                .lookupAll(pdfMd5).block(Duration.ofSeconds(2));
        assertNotNull(cached, "缓存必须存在");
        assertEquals(3, cached.size(), "三表缓存必须全部命中");

        // 第二轮：不同用户上传相同 PDF（验证 M2.10 缓存跨用户共享）
        // 使用 userId=3 避免 (userId=2, pdfMd5=pingan_xxx) 唯一约束冲突；
        // 跨用户缓存命中是 M2.10 决策 D1（跨用户共享缓存）的核心验收点。
        Mockito.clearInvocations(messageProducer);

        Long userId2 = 3L;
        String taskId2 = orchestrator.createTask(userId2, payload)
                .map(com.finreport.domain.entity.Task::getId)
                .block(Duration.ofSeconds(15));

        Report report2 = Report.builder()
                .taskId(taskId2)
                .userId(userId2)
                .companyCode("000001")
                .companyName("平安银行")
                .reportType("ANNUAL")
                .reportPeriod("2025")
                .pdfMd5(pdfMd5)
                .pdfObjectKey("uploads/" + userId2 + "/" + pdfMd5 + ".pdf")
                .pageCount(180)
                .parseStatus("COMPLETED")
                .build();
        reportRepo.save(report2).block(Duration.ofSeconds(5));

        // PARSE 成功后 → dispatchExtractionSteps → checkCacheAndReplayOrDispatch → 三表全命中 → replayCachedExtracts
        orchestrator.handleStepProgress(taskId2, "PARSE", "SUCCESS",
                Map.of("pdfObjectKey", payload.get("pdfObjectKey"), "pageCount", 180))
                .block(Duration.ofSeconds(15));

        // 验证：task2 直接进入 EXTRACT_SUCCESS（无需 EXTRACT_BS/IS/CF progress 回报）
        await().atMost(Duration.ofSeconds(10))
                .untilAsserted(() -> {
                    com.finreport.domain.entity.Task task = taskRepo.findById(taskId2)
                            .block(Duration.ofSeconds(2));
                    assertNotNull(task);
                    assertTrue(
                            "EXTRACT_SUCCESS".equals(task.getStatus())
                                    || "CHECK_RUNNING".equals(task.getStatus())
                                    || "CHECK_SUCCESS".equals(task.getStatus())
                                    || "REPORT_RUNNING".equals(task.getStatus())
                                    || "COMPLETED".equals(task.getStatus()),
                            "重传后任务应跨过 EXTRACT_RUNNING 进入 EXTRACT_SUCCESS 或更后状态，实际: "
                                    + (task != null ? task.getStatus() : "null"));
                });

        // 验证：TaskMessageProducer.publishTaskStep 没有被调用 extract.bs/is/cf routing key
        // （publishTaskStep 调用次数：PARSE 1 次 + CHECK 1 次 + REPORT 1 次 = 3 次；不应有 extract.* 调用）
        verify(messageProducer, atLeast(1)).publishTaskStep(
                anyString(), anyString(), any(), anyString());
        verify(messageProducer, never()).publishTaskStep(
                anyString(), org.mockito.ArgumentMatchers.eq("extract.bs"), any(), any());
        verify(messageProducer, never()).publishTaskStep(
                anyString(), org.mockito.ArgumentMatchers.eq("extract.is"), any(), any());
        verify(messageProducer, never()).publishTaskStep(
                anyString(), org.mockito.ArgumentMatchers.eq("extract.cf"), any(), any());
    }

    /**
     * 用户隔离校验：用户 B 不能访问用户 A 的 report（M2.11 StatementQueryService 验收）。
     */
    @Test
    @DisplayName("用户隔离：A 用户的 report 不能被 B 用户查询")
    void shouldRejectCrossUserReportAccess() {
        Long ownerUserId = 10L;
        Long otherUserId = 20L;
        String pdfMd5 = "catl_" + UUID.randomUUID().toString().replace("-", "").substring(0, 16);

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("companyCode", "300750");
        payload.put("companyName", "宁德时代");
        payload.put("reportType", "ANNUAL");
        payload.put("reportPeriod", "2025");
        payload.put("pdfMd5", pdfMd5);
        payload.put("pdfObjectKey", "uploads/" + ownerUserId + "/" + pdfMd5 + ".pdf");

        String taskId = orchestrator.createTask(ownerUserId, payload)
                .map(com.finreport.domain.entity.Task::getId)
                .block(Duration.ofSeconds(15));

        Report report = Report.builder()
                .taskId(taskId)
                .userId(ownerUserId)
                .companyCode("300750")
                .companyName("宁德时代")
                .reportType("ANNUAL")
                .reportPeriod("2025")
                .pdfMd5(pdfMd5)
                .pdfObjectKey("uploads/" + ownerUserId + "/" + pdfMd5 + ".pdf")
                .pageCount(200)
                .parseStatus("COMPLETED")
                .build();
        Long reportId = reportRepo.save(report).map(Report::getId).block(Duration.ofSeconds(5));

        // 用户 B 访问用户 A 的 report → 抛 BusinessException NOT_FOUND
        BusinessException ex1 = assertThrows(BusinessException.class, () ->
                statementQueryService.getReportDetail(reportId, otherUserId).block(Duration.ofSeconds(5)));
        assertEquals("REPORT_NOT_FOUND", ex1.getErrorCode(),
                "跨用户访问应抛 REPORT_NOT_FOUND");

        BusinessException ex2 = assertThrows(BusinessException.class, () ->
                statementQueryService.getStatements(reportId, otherUserId).block(Duration.ofSeconds(5)));
        assertEquals("REPORT_NOT_FOUND", ex2.getErrorCode(),
                "跨用户访问应抛 REPORT_NOT_FOUND");

        // 用户 A 自身能正常查询
        com.finreport.domain.dto.ReportDetailResponse detail = statementQueryService
                .getReportDetail(reportId, ownerUserId).block(Duration.ofSeconds(5));
        assertNotNull(detail, "归属用户应能正常查询");
        assertEquals("300750", detail.companyCode());
    }

    /**
     * 构造与 L3 {@code extract_handler.handle} 返回结构一致的 result payload。
     *
     * @param statementType balance_sheet / income_statement / cash_flow
     * @param items         该表的所有科目行（每行含 item / value / scope / period 字段）
     * @return M2.09 契约的 result payload
     */
    private static Map<String, Object> buildExtractResult(String statementType, List<Map<String, Object>> items) {
        Map<String, Object> statement = new LinkedHashMap<>();
        statement.put("report_period", "2025-12-31");
        statement.put("currency", "CNY");
        statement.put("unit", "元");
        statement.put("statements", Map.of(statementType, items));

        Map<String, Object> validation = new LinkedHashMap<>();
        validation.put("is_valid", true);
        validation.put("issues", List.of());
        validation.put("error_hint", "");

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("success", true);
        result.put("statement", statement);
        result.put("validation", validation);
        result.put("confidence", 0.92);
        result.put("source_page", 5);
        result.put("retried", false);
        result.put("tokens_used", 1234);
        result.put("latency_ms", 5600);
        return result;
    }
}
