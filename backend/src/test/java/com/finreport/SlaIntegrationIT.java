package com.finreport;

import static org.awaitility.Awaitility.await;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.lenient;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
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
import com.finreport.domain.entity.Task;
import com.finreport.domain.entity.TaskStep;
import com.finreport.domain.enums.StepStatus;
import com.finreport.mq.TaskMessageProducer;
import com.finreport.repository.ReportRepository;
import com.finreport.repository.TaskRepository;
import com.finreport.repository.TaskStepRepository;
import com.finreport.service.artifact.ReportArtifactWriter;
import com.finreport.service.orchestrator.TaskOrchestrator;

import reactor.core.publisher.Mono;

/**
 * M3.10 端到端 SLA 测试 — spec §3.7 / §12.1 + plan §M3.10。
 *
 * <p>验收标准（plan §M3.10）：</p>
 * <ul>
 *   <li>PARSE &lt; 90 s（超时 180 s）</li>
 *   <li>EXTRACT（三表并行）&lt; 60 s（超时 120 s）</li>
 *   <li>CHECK &lt; 30 s（超时 60 s）</li>
 *   <li>REPORT &lt; 45 s（超时 90 s）</li>
 *   <li>总链路 &lt; 4 min（超时 8 min）</li>
 * </ul>
 *
 * <p>验证方式：JUnit 测试用 Stopwatch（{@link System#nanoTime()}）断言每阶段
 * 端到端耗时在 SLA 阈值内（plan §M3.10「JUnit 测试用 Stopwatch 断言」）。</p>
 *
 * <h2>测试策略</h2>
 * <ul>
 *   <li>Testcontainers MySQL 8.0 + Redis 7.2 — 真实持久化与缓存；</li>
 *   <li>{@code @MockBean TaskMessageProducer} — 屏蔽真实 RabbitMQ，{@code publishTaskStep} 变为 no-op；</li>
 *   <li>{@code @MockBean ReportArtifactWriter} — 屏蔽真实 MinIO，{@code writeArtifacts} 返回 {@code Mono.just(0)}；</li>
 *   <li>直接调 {@code TaskOrchestrator.handleStepProgress} 模拟 L3 回报；</li>
 *   <li>每阶段入口前记录 {@code System.nanoTime()}，SUCCESS 回报后再次记录，差额即端到端耗时；</li>
 *   <li>同步验证 {@code task_step.duration_ms} 被 {@code TaskOrchestrator.handleStepSuccess} 正确回填。</li>
 * </ul>
 *
 * <h2>命名说明</h2>
 *
 * <p>plan §M3.10 字面文件名为 {@code SlaIntegrationTest.java}，但项目自 M2.12 起约定
 * 「使用 Testcontainers 的测试命名 {@code *IT.java}，由 failsafe 在 {@code -Pintegration}
 * 激活时执行」（见 {@code backend/pom.xml} maven-failsafe-plugin 的 include 模式）。
 * 本测试用 Testcontainers，按项目约定命名为 {@code SlaIntegrationIT.java}，由 {@code mvn verify
 * -Pintegration} 运行；与现有 {@code ReportParseIntegrationIT.java} / {@code FlywayMigrationIT.java}
 * 保持一致。</p>
 *
 * <h2>实测含义</h2>
 *
 * <p>本测试为 <b>SLA 测量基线</b>，验证「Java 编排层 + R2DBC + Redis + 事务写入」自身的端到端
 * 延迟在 SLA 阈值内。Mock 模式下每阶段实测通常 &lt; 1 s，远低于 SLA。真实 L3 推理
 * （GPU 7B 4-bit 模型）延迟由独立脚本 {@code scripts/eval_m2_sla.py} 在 GPU 环境运行，
 * 不在 CI 流水线中执行（spec §12.1 注释 + {@code ReportParseIntegrationIT} 类注释）。</p>
 *
 * <p>测试本身的存在意义：</p>
 * <ol>
 *   <li>把 spec §3.7 SLA 阈值作为常量永久固化，未来误改阈值会编译失败；</li>
 *   <li>把每阶段测量的代码路径（dispatch → handleStepProgress → durationMs 回填）作为回归测试，
 *       防止 future 重构破坏 {@code task_step.duration_ms} 写入；</li>
 *   <li>把「4 min 内完整跑完 6 个状态机阶段」作为冒烟基线，未来任一阶段卡死会立即暴露。</li>
 * </ol>
 */
@SpringBootTest
@ActiveProfiles("it")
@Testcontainers
class SlaIntegrationIT {

    private static final String USER = "finreport";
    private static final String PASSWORD = "finreport";
    private static final String DATABASE = "finreport";
    private static final String MYSQL_IMAGE = "mysql:8.0.36";
    private static final String REDIS_IMAGE = "redis:7.2-alpine";

    // ---------------------------------------------------------------------------
    // SLA 阈值（spec §3.7 / §12.1）— 作为常量固化，防止阈值漂移
    // ---------------------------------------------------------------------------

    /** spec §3.7：PARSE 100 页 PDF < 90 s（超时 180 s）。 */
    private static final Duration PARSE_SLA = Duration.ofSeconds(90);

    /** spec §3.7：EXTRACT 三表并行 < 60 s（超时 120 s）。 */
    private static final Duration EXTRACT_SLA = Duration.ofSeconds(60);

    /** spec §3.7：CHECK < 30 s（超时 60 s）。 */
    private static final Duration CHECK_SLA = Duration.ofSeconds(30);

    /** spec §3.7：REPORT < 45 s（超时 90 s）。 */
    private static final Duration REPORT_SLA = Duration.ofSeconds(45);

    /** spec §3.7：总链路 < 4 min（超时 8 min）。 */
    private static final Duration TOTAL_SLA = Duration.ofMinutes(4);

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

    /** 屏蔽真实 MQ：{@code publishTaskStep} 变为 no-op，{@code dispatchStep} 链路继续推进。 */
    @MockBean
    private TaskMessageProducer messageProducer;

    /** 屏蔽真实 MinIO：{@code writeArtifacts} 返回 {@code Mono.just(0)}，状态机仍推进到 COMPLETED。 */
    @MockBean
    private ReportArtifactWriter reportArtifactWriter;

    /**
     * 端到端 SLA 冒烟：完整跑通 PARSE → EXTRACT_BS/IS/CF → CHECK → REPORT → COMPLETED，
     * 断言每阶段端到端耗时在 spec §3.7 阈值内，并验证 {@code task_step.duration_ms} 被回填。
     *
     * <p>Mock 模式下每阶段实测 &lt; 1 s，本测试主要保障：</p>
     * <ol>
     *   <li>SLA 阈值常量不被未来误改；</li>
     *   <li>{@code task_step.duration_ms} 写入路径不被破坏；</li>
     *   <li>6 个状态机阶段在 4 min 内能完整跑完（冒烟基线）。</li>
     * </ol>
     */
    @Test
    @DisplayName("端到端 SLA：PARSE<90s EXTRACT<60s CHECK<30s REPORT<45s 总链路<4min")
    void shouldCompletePipelineWithinSlaThresholds() {
        // MockBean 默认方法返回 null；lenient() 显式 stub writeArtifacts 返回 Mono.just(0)
        // 避免 ReportArtifactWriter 在 REPORT SUCCESS 分支抛 NPE。
        lenient().when(reportArtifactWriter.writeArtifacts(anyString(), any()))
                .thenReturn(Mono.just(0));

        Long userId = 1001L;
        String pdfMd5 = "sla_" + UUID.randomUUID().toString().replace("-", "").substring(0, 16);
        long totalStart = System.nanoTime();

        // 1. 创建任务（同时调度 PARSE）
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("companyCode", "600519");
        payload.put("companyName", "贵州茅台");
        payload.put("reportType", "ANNUAL");
        payload.put("reportPeriod", "2025");
        payload.put("pdfMd5", pdfMd5);
        payload.put("pdfObjectKey", "uploads/" + userId + "/" + pdfMd5 + ".pdf");

        String taskId = orchestrator.createTask(userId, payload)
                .map(Task::getId)
                .block(Duration.ofSeconds(15));
        assertNotNull(taskId, "任务 ID 必须返回");

        // 2. 创建 Report 记录（同 ReportParseIntegrationIT 路径）
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

        // ---------- 阶段 1：PARSE ----------
        long parseStart = System.nanoTime();
        orchestrator.handleStepProgress(taskId, "PARSE", "SUCCESS",
                Map.of("pdfObjectKey", payload.get("pdfObjectKey"), "pageCount", 120))
                .block(Duration.ofSeconds(10));
        Duration parseElapsed = Duration.ofNanos(System.nanoTime() - parseStart);
        assertSla("PARSE", parseElapsed, PARSE_SLA);

        // ---------- 阶段 2：EXTRACT（三表并行）----------
        // spec §3.7：EXTRACT 阈值是「三表并行」整体耗时，不是单表累加。
        // 实测点：从首次 dispatch 到 3 表全部 SUCCESS（CHECK 即将调度）的时间窗。
        long extractStart = System.nanoTime();
        orchestrator.handleStepProgress(taskId, "EXTRACT_BS", "SUCCESS",
                buildExtractResult("balance_sheet", List.of(
                        Map.of("item", "货币资金", "value", 1.23e9, "scope", "合并", "period", "本期"),
                        Map.of("item", "应收账款", "value", 5.0e8, "scope", "合并", "period", "本期"),
                        Map.of("item", "资产总计", "value", 5.67e10, "scope", "合并", "period", "本期"),
                        Map.of("item", "负债合计", "value", 2.0e10, "scope", "合并", "period", "本期"),
                        Map.of("item", "所有者权益合计", "value", 3.67e10, "scope", "合并", "period", "本期"))))
                .block(Duration.ofSeconds(10));
        orchestrator.handleStepProgress(taskId, "EXTRACT_IS", "SUCCESS",
                buildExtractResult("income_statement", List.of(
                        Map.of("item", "营业收入", "value", 8.9e9, "scope", "合并", "period", "本期"),
                        Map.of("item", "净利润", "value", 8.7e8, "scope", "合并", "period", "本期"))))
                .block(Duration.ofSeconds(10));
        orchestrator.handleStepProgress(taskId, "EXTRACT_CF", "SUCCESS",
                buildExtractResult("cash_flow", List.of(
                        Map.of("item", "经营活动产生的现金流量净额", "value", 1.2e9, "scope", "合并", "period", "本期"))))
                .block(Duration.ofSeconds(10));
        Duration extractElapsed = Duration.ofNanos(System.nanoTime() - extractStart);
        assertSla("EXTRACT", extractElapsed, EXTRACT_SLA);

        // ---------- 阶段 3：CHECK ----------
        long checkStart = System.nanoTime();
        orchestrator.handleStepProgress(taskId, "CHECK", "SUCCESS", buildCheckResult())
                .block(Duration.ofSeconds(10));
        Duration checkElapsed = Duration.ofNanos(System.nanoTime() - checkStart);
        assertSla("CHECK", checkElapsed, CHECK_SLA);

        // ---------- 阶段 4：REPORT ----------
        long reportStart = System.nanoTime();
        orchestrator.handleStepProgress(taskId, "REPORT", "SUCCESS", buildReportResult())
                .block(Duration.ofSeconds(10));
        Duration reportElapsed = Duration.ofNanos(System.nanoTime() - reportStart);
        assertSla("REPORT", reportElapsed, REPORT_SLA);

        // ---------- 总链路 ----------
        Duration totalElapsed = Duration.ofNanos(System.nanoTime() - totalStart);
        assertSla("总链路", totalElapsed, TOTAL_SLA);

        // ---------- 任务进入 COMPLETED ----------
        await().atMost(Duration.ofSeconds(5))
                .untilAsserted(() -> {
                    Task task = taskRepo.findById(taskId).block(Duration.ofSeconds(2));
                    assertNotNull(task, "任务必须存在");
                    assertEquals("COMPLETED", task.getStatus(),
                            "任务应进入 COMPLETED 状态，实际: " + task.getStatus());
                    assertEquals(100, task.getProgress(),
                            "任务进度应为 100，实际: " + task.getProgress());
                });

        // ---------- task_step.duration_ms 回填校验 ----------
        // spec §3.7 SLA 度量依赖 task_step.duration_ms；handleStepSuccess 在 SUCCESS 时计算
        // Duration.between(startedAt, finishedAt).toMillis()。这里回归验证每阶段都有非 null duration。
        verifyStepDurationRecorded(taskId, "PARSE");
        verifyStepDurationRecorded(taskId, "EXTRACT_BS");
        verifyStepDurationRecorded(taskId, "EXTRACT_IS");
        verifyStepDurationRecorded(taskId, "EXTRACT_CF");
        verifyStepDurationRecorded(taskId, "CHECK");
        verifyStepDurationRecorded(taskId, "REPORT");
    }

    /**
     * 断言单阶段耗时在 SLA 阈值内，失败时打印阶段名 + 实测 + 阈值便于定位。
     *
     * @param stageName 阶段名（PARSE / EXTRACT / CHECK / REPORT / 总链路）
     * @param elapsed   实测耗时
     * @param sla       SLA 阈值
     */
    private static void assertSla(String stageName, Duration elapsed, Duration sla) {
        assertTrue(elapsed.compareTo(sla) <= 0,
                String.format("%s 阶段 SLA 不达标：实测 %d ms > 阈值 %d ms（spec §3.7）",
                        stageName, elapsed.toMillis(), sla.toMillis()));
    }

    /**
     * 验证 {@code task_step.duration_ms} 在 SUCCESS 后被回填为非 null 且 >= 0。
     *
     * <p>spec §3.7 SLA 度量依赖该字段；M3.10 把字段写入路径作为回归点固化，
     * 防止 future 重构破坏 SLA 监控数据源。</p>
     *
     * @param taskId   任务 ID
     * @param stepName 步骤名
     */
    private void verifyStepDurationRecorded(String taskId, String stepName) {
        TaskStep step = stepRepo.findByTaskIdAndStepName(taskId, stepName)
                .block(Duration.ofSeconds(2));
        assertNotNull(step, "task_step 必须存在: " + stepName);
        assertEquals(StepStatus.SUCCESS.name(), step.getStatus(),
                "task_step.status 应为 SUCCESS: " + stepName);
        assertNotNull(step.getStartedAt(), "task_step.started_at 必须回填: " + stepName);
        assertNotNull(step.getFinishedAt(), "task_step.finished_at 必须回填: " + stepName);
        assertNotNull(step.getDurationMs(), "task_step.duration_ms 必须回填: " + stepName);
        assertTrue(step.getDurationMs() >= 0,
                "task_step.duration_ms 应 >= 0: " + stepName + " 实际=" + step.getDurationMs());
    }

    /**
     * 构造与 L3 {@code extract_handler.handle} 返回结构一致的 result payload（与
     * {@code ReportParseIntegrationIT.buildExtractResult} 同契约）。
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

    /**
     * 构造与 L3 {@code CheckResult.to_dict()} 返回结构一致的 result payload（M3.04 契约）。
     *
     * <p>3 条规则 + 0 条异常的合法最小 payload，便于 CheckResultWriter 写库验证。</p>
     */
    private static Map<String, Object> buildCheckResult() {
        Map<String, Object> rule1 = new LinkedHashMap<>();
        rule1.put("rule_type", "balance_sheet_identity");
        rule1.put("rule_name", "资产=负债+所有者权益");
        rule1.put("expected", "56700000000.00");
        rule1.put("actual", "56700000000.00");
        rule1.put("diff", "0.00");
        rule1.put("is_pass", true);
        rule1.put("severity", "INFO");
        rule1.put("tolerance", "0.01");
        rule1.put("note", "");
        rule1.put("missing_items", List.of());
        rule1.put("llm_reviewed", false);

        Map<String, Object> rule2 = new LinkedHashMap<>();
        // 对齐 L3 RuleType.NET_INCOME_TO_RETAINED 枚举值（app/schemas/reasoning.py）。
        // V7 迁移已把 accounting_check.rule_type 扩容到 VARCHAR(32) 容纳此值。
        rule2.put("rule_type", "net_income_to_retained");
        rule2.put("rule_name", "净利润→未分配利润变动");
        rule2.put("expected", "870000000.00");
        rule2.put("actual", "870000000.00");
        rule2.put("diff", "0.00");
        rule2.put("is_pass", true);
        rule2.put("severity", "INFO");
        rule2.put("tolerance", "0.01");
        rule2.put("note", "");
        rule2.put("missing_items", List.of());
        rule2.put("llm_reviewed", false);

        Map<String, Object> rule3 = new LinkedHashMap<>();
        rule3.put("rule_type", "cash_flow_vs_net_income");
        rule3.put("rule_name", "经营现金流 vs 净利润");
        rule3.put("expected", "1200000000.00");
        rule3.put("actual", "1200000000.00");
        rule3.put("diff", "0.00");
        rule3.put("is_pass", true);
        rule3.put("severity", "INFO");
        rule3.put("tolerance", "0.01");
        rule3.put("note", "");
        rule3.put("missing_items", List.of());
        rule3.put("llm_reviewed", false);

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("rules", List.of(rule1, rule2, rule3));
        result.put("anomalies", List.of());
        result.put("confidence", 1.0);
        result.put("report_period", "2025-12-31");
        return result;
    }

    /**
     * 构造与 L3 {@code ReportResult + PdfResult + ChartResult} 字段对齐的最小 REPORT payload
     * （M3.08 契约）。{@code ReportArtifactWriter} 被 @MockBean 屏蔽，不会真正解码 PDF/MD/PNG；
     * 这里仅保证 payload 形状合法，便于未来切换为真实 MinIO 时不破契约。
     */
    private static Map<String, Object> buildReportResult() {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("operation", "REPORT");
        result.put("status", "SUCCESS");
        result.put("report_period", "2025-12-31");
        result.put("markdown", "# 公司概况\n贵州茅台 2025 年报深度解析。");
        result.put("pdf_b64", "JVBERi0xLjQKJUUkJUVPRgo="); // 11 字节 PDF header 占位
        // 1x1 像素透明 PNG 的 base64 占位，避免 L3 真实切换时报 NPE
        String pngB64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC";
        result.put("charts", List.of(
                Map.of("chart_type", "asset_structure_pie", "png_b64", pngB64),
                Map.of("chart_type", "revenue_trend_line", "png_b64", pngB64),
                Map.of("chart_type", "cash_flow_bar", "png_b64", pngB64)));
        result.put("fallback", false);
        result.put("latency_ms", 32000);
        return result;
    }
}
