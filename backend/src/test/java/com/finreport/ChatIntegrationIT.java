package com.finreport;

import static org.awaitility.Awaitility.await;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.doReturn;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;

import java.io.IOException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfSystemProperty;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.boot.test.mock.mockito.SpyBean;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.MySQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.utility.DockerImageName;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.finreport.domain.dto.ChatDtos.ChatSessionResponse;
import com.finreport.domain.dto.ChatDtos.CreateSessionRequest;
import com.finreport.domain.dto.ChatDtos.SendMessageRequest;
import com.finreport.domain.entity.ChatMessage;
import com.finreport.domain.entity.Report;
import com.finreport.mq.ChatMessageProducer;
import com.finreport.repository.ChatMessageRepository;
import com.finreport.repository.ReportRepository;
import com.finreport.service.ChatService;
import com.finreport.service.sse.ChatStreamProxy;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * M5.09 问答链路集成测试 — plan §M5.09（spec §6.3.3）。
 *
 * <p>验收标准：5 个标准问题关键事实命中率 ≥ 0.85；首 token &lt; 15 s。
 * 基准数据：{@code data/benchmark/qa_questions.json}（茅台 2025 年报，dev
 * 库 report_id=17 真实抽取值 + 知识库第 15 页真实内容）。</p>
 *
 * <h2>双模式设计</h2>
 *
 * <p>问答链路数据面 = L2 SSE 透传 + L3 ReAct（DeepSeek API + 工具）。L3 真实
 * 推理依赖 dev 栈（ai-service + Milvus 知识库 + LLM_API_KEY），不能在 CI 常态化
 * 运行。因此本测试分两种模式（同一 Spring 上下文，{@code @SpyBean} 切换）：</p>
 *
 * <ol>
 *   <li><b>Mock 模式（默认，{@code mvn verify -Pintegration} 自动执行）</b>：
 *       {@link ChatStreamProxy} 以 Spy 注入 canned 事件流，屏蔽真实 L3，验证
 *       L2 链路机制：会话 CRUD + 归属校验、user/assistant 消息落库、SSE 事件
 *       透传顺序、done 落库（tokenCount/toolsUsed）、首 token 延迟 &lt; 15 s
 *       （SLA 常量固化，spec §12.1）、error 事件透传不落库、MQ 控制流发布。</li>
 *   <li><b>Real 模式（验收，{@code -Dchat.it.real-l3=true}）</b>：不 stub，走
 *       真实 L3（dev 栈），逐问执行 5 个标准问题，断言关键事实命中率 ≥ 0.85
 *       且每问首 token &lt; 15 s。需要 dev 栈运行 + LLM_API_KEY，验收时手动
 *       执行（同 M2.12 eval 脚本的定位，不占用 CI）。</li>
 * </ol>
 *
 * <p><b>命名说明</b>：plan §M5.09 字面文件名为 {@code ChatIntegrationTest.java}，
 * 但项目自 M2.12 起约定「使用 Testcontainers 的测试命名 {@code *IT.java}，由
 * failsafe 在 {@code -Pintegration} 激活时执行」（见 {@code SlaIntegrationIT}），
 * 本测试沿用该约定。</p>
 *
 * <p><b>首 token 口径</b>：spec §12.1「首 token &lt; 15 s」在项目内以「首个生成
 * 事件」度量——L3 事件流先发 thought（LLM 首段生成）再发最终答案 token
 * （见 {@code ai-service/app/api/chat.py}「首个 thought 事件远早于最终答案，
 * 满足首 token &lt; 15 s」注释）。本测试对 SSE 流首个事件计时，与 M5.04 冒烟
 * 口径一致。</p>
 */
@SpringBootTest
@ActiveProfiles("it")
@Testcontainers
class ChatIntegrationIT {

    private static final Logger log = LoggerFactory.getLogger(ChatIntegrationIT.class);

    private static final String USER = "finreport";
    private static final String PASSWORD = "finreport";
    private static final String DATABASE = "finreport";
    private static final String MYSQL_IMAGE = "mysql:8.0.36";
    private static final String REDIS_IMAGE = "redis:7.2-alpine";

    /** spec §12.1 问答首 token SLA（超时 30 s）。 */
    private static final Duration FIRST_TOKEN_SLA = Duration.ofSeconds(15);

    /** 问答每问整流超时：L3 120 s 整流 + 网络余量。 */
    private static final Duration PER_QUESTION_TIMEOUT = Duration.ofSeconds(180);

    /**
     * 命中率/首 token 门槛不在此硬编码：以 {@code qa_questions.json} acceptance
     * 块为唯一事实来源（loadBenchmark 读取），避免基准与断言双源漂移。
     */

    /** real 模式开关（JUnit 条件 + 本类逻辑共用）。 */
    private static final String REAL_L3_PROPERTY = "chat.it.real-l3";

    /** benchmark 问题文件路径（相对 backend 模块工作目录），可用系统属性覆盖。 */
    private static final String BENCHMARK_PATH_PROPERTY = "chat.it.benchmark";
    private static final String DEFAULT_BENCHMARK_PATH = "../data/benchmark/qa_questions.json";

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
    private ChatService chatService;

    @Autowired
    private ChatMessageRepository messageRepository;

    @Autowired
    private ReportRepository reportRepository;

    @Autowired
    private org.springframework.r2dbc.core.DatabaseClient databaseClient;

    /** 屏蔽真实 MQ 控制流（spec §3.3：控制面独立于数据面，发布失败不阻断 SSE）。 */
    @MockBean
    private ChatMessageProducer messageProducer;

    /** L3 边界 Spy：mock 模式 stub canned 流；real 模式不 stub，走真实 L3。 */
    @SpyBean
    private ChatStreamProxy streamProxy;

    // ========================================================================
    // Mock 模式（默认，CI 自动执行）
    // ========================================================================

    /**
     * 会话 CRUD + 归属校验：创建、列表、历史、删除，以及越权访问返回
     * SESSION_NOT_FOUND（spec §8.5 按用户隔离）。
     */
    @Test
    @DisplayName("会话 CRUD：创建/列表/历史/删除 + 归属校验")
    void shouldManageSessionsWithOwnershipCheck() {
        Long userId = 1001L;
        Long reportId = saveReport(userId);

        ChatSessionResponse session = chatService.createSession(userId,
                new CreateSessionRequest(reportId, "茅台问答"))
                .block(Duration.ofSeconds(5));
        assertNotNull(session, "会话必须创建成功");
        assertEquals(reportId, session.reportId());
        assertEquals("茅台问答", session.title());

        // 列表包含新会话
        List<ChatSessionResponse> sessions = chatService.listSessions(userId)
                .collectList().block(Duration.ofSeconds(5));
        assertNotNull(sessions);
        assertTrue(sessions.stream().anyMatch(s -> s.id().equals(session.id())), "会话列表应包含新会话");

        // 越权：其他用户访问该会话 → SESSION_NOT_FOUND
        String ownerCheckError = chatService.listMessages(session.id(), 9999L)
                .collectList()
                .map(ignored -> "ok")
                .onErrorResume(error -> Mono.just(error.getMessage()))
                .block(Duration.ofSeconds(5));
        assertNotNull(ownerCheckError);
        assertTrue(ownerCheckError.contains("会话不存在"), "越权应返回会话不存在，实际: " + ownerCheckError);

        // 删除后列表为空
        chatService.deleteSession(session.id(), userId).block(Duration.ofSeconds(5));
        List<ChatSessionResponse> afterDelete = chatService.listSessions(userId)
                .collectList().block(Duration.ofSeconds(5));
        assertNotNull(afterDelete);
        assertTrue(afterDelete.stream().noneMatch(s -> s.id().equals(session.id())), "删除后会话不应存在");
    }

    /**
     * SSE 事件透传 + 消息落库 + 首 token SLA + MQ 控制流（spec §6.3.3 事件序）。
     *
     * <p>验证：thought → tool_call → tool_result → token → token → done 顺序
     * 原样透传；done 后 assistant 消息落库（content=token 累计、toolsUsed、
     * tokenCount）；首事件（thought）延迟 &lt; 15 s（spec §12.1）；MQ 控制流
     * 恰好发布 1 次。</p>
     */
    @Test
    @DisplayName("SSE 事件透传：thought→tool_call→tool_result→token→done + 落库 + 首token<15s")
    void shouldPassthroughSseEventsAndPersistMessages() {
        Long userId = 1001L;
        Long reportId = saveReport(userId);
        ChatSessionResponse session = chatService.createSession(userId,
                new CreateSessionRequest(reportId, null)).block(Duration.ofSeconds(5));
        assertNotNull(session, "会话必须创建成功");

        // 注意：traceId 来自 MDC，测试线程为 null，必须用 any()（anyString 不匹配 null）。
        doReturn(cannedEvents()).when(streamProxy).stream(any(), any());

        long start = System.nanoTime();
        AtomicReference<Duration> firstEventLatency = new AtomicReference<>();
        List<ServerSentEvent<String>> events = chatService.sendMessage(session.id(), userId,
                new SendMessageRequest("贵州茅台2025年末资产总计是多少？"))
                .doOnNext(event -> firstEventLatency.compareAndSet(
                        null, Duration.ofNanos(System.nanoTime() - start)))
                .collectList()
                .block(Duration.ofSeconds(15));
        assertNotNull(events, "必须收到事件流");

        // 事件序：thought → tool_call → tool_result → token → token → done
        List<String> eventNames = events.stream().map(e -> e.event()).toList();
        assertEquals(List.of("thought", "tool_call", "tool_result", "token", "token", "done"),
                eventNames, "SSE 事件顺序应匹配 spec §6.3.3");

        // 首 token SLA（spec §12.1：< 15s）
        Duration firstLatency = firstEventLatency.get();
        assertNotNull(firstLatency, "必须记录首事件延迟");
        assertTrue(firstLatency.compareTo(FIRST_TOKEN_SLA) <= 0,
                String.format("首 token SLA 不达标：实测 %d ms > %d ms（spec §12.1）",
                        firstLatency.toMillis(), FIRST_TOKEN_SLA.toMillis()));

        // MQ 控制流恰好发布 1 次（消息归属校验通过后）；traceId 为 null，用 any()
        verify(messageProducer, times(1)).publishChat(any(), any());

        // done 后 assistant 消息落库：内容 = token 累计，工具与 token 数来自 done 元数据
        await().atMost(Duration.ofSeconds(5)).untilAsserted(() -> {
            List<ChatMessage> messages = messageRepository
                    .findBySessionIdOrderByCreatedAtDesc(session.id())
                    .collectList().block(Duration.ofSeconds(2));
            assertNotNull(messages);
            ChatMessage userMsg = messages.stream()
                    .filter(m -> "user".equals(m.getRole())).findFirst().orElse(null);
            assertNotNull(userMsg, "用户消息必须落库");
            assertEquals("贵州茅台2025年末资产总计是多少？", userMsg.getContent());

            ChatMessage assistant = messages.stream()
                    .filter(m -> "assistant".equals(m.getRole())).findFirst().orElse(null);
            assertNotNull(assistant, "assistant 消息必须落库");
            assertEquals("贵州茅台2025年末资产总计为3038.35亿元。", assistant.getContent(),
                    "答案应为 token 事件累计文本");
            assertEquals(List.of("query_statement"), parseTools(assistant.getToolsUsed()),
                    "tools_used 应来自 done 元数据");
            assertEquals(2, assistant.getTokenCount(), "token_count 应来自 done 元数据");
        });
    }

    /**
     * L3 error 事件：原样透传前端，不落库 assistant 消息（spec §6.3.3 错误语义）。
     */
    @Test
    @DisplayName("L3 error 事件透传且不落库 assistant")
    void shouldForwardErrorEventWithoutPersisting() {
        Long userId = 1001L;
        Long reportId = saveReport(userId);
        ChatSessionResponse session = chatService.createSession(userId,
                new CreateSessionRequest(reportId, null)).block(Duration.ofSeconds(5));
        assertNotNull(session, "会话必须创建成功");

        doReturn(Flux.just(sse("error", "{\"code\":\"CHAT_FAILED\",\"message\":\"LLM 超时\"}")))
                .when(streamProxy).stream(any(), any());

        List<ServerSentEvent<String>> events = chatService.sendMessage(session.id(), userId,
                new SendMessageRequest("测试错误"))
                .collectList().block(Duration.ofSeconds(10));
        assertNotNull(events);
        assertEquals(1, events.size());
        assertEquals("error", events.get(0).event());

        // 不落库 assistant 消息
        await().atMost(Duration.ofSeconds(3)).untilAsserted(() -> {
            List<ChatMessage> messages = messageRepository
                    .findBySessionIdOrderByCreatedAtDesc(session.id())
                    .collectList().block(Duration.ofSeconds(2));
            assertNotNull(messages);
            assertTrue(messages.stream().noneMatch(m -> "assistant".equals(m.getRole())),
                    "error 事件不得落库 assistant 消息");
        });
    }

    /**
     * 空消息拒绝：返回业务异常而非进入链路（接口防御）。
     */
    @Test
    @DisplayName("空消息返回 CHAT_CONTENT_EMPTY")
    void shouldRejectEmptyContent() {
        Long userId = 1001L;
        Long reportId = saveReport(userId);
        ChatSessionResponse session = chatService.createSession(userId,
                new CreateSessionRequest(reportId, null)).block(Duration.ofSeconds(5));
        assertNotNull(session, "会话必须创建成功");

        String error = chatService.sendMessage(session.id(), userId,
                new SendMessageRequest("   "))
                .collectList()
                .map(ignored -> "ok")
                .onErrorResume(t -> Mono.just(t.getMessage()))
                .block(Duration.ofSeconds(5));
        assertNotNull(error);
        assertTrue(error.contains("消息内容不能为空"), "空消息应拒绝，实际: " + error);
    }

    // ========================================================================
    // Real 模式（验收：-Dchat.it.real-l3=true，需要 dev 栈 + LLM_API_KEY）
    // ========================================================================

    /**
     * 5 个标准问题命中率验收（plan §M5.09）。
     *
     * <p>前置条件：dev 栈运行（ai-service + MySQL + Milvus 知识库 + LLM_API_KEY），
     * 且 dev 库 report_id=17 为贵州茅台 2025 真实抽取数据、知识库含 3 份 sample
     * （茅台 doc_id=1）。benchmark 文件 {@code data/benchmark/qa_questions.json}
     * 的 key_facts 由这些真实数据推导，任何一方漂移都会在此暴露。</p>
     *
     * <p>验收断言：命中率 ≥ 0.85；每问首事件延迟 &lt; 15 s；失败时输出逐问
     * 明细（问题/答案/命中事实/延迟）便于定位。</p>
     */
    @Test
    @DisplayName("Real L3：5 个标准问题命中率 ≥ 0.85 且首 token < 15s")
    @EnabledIfSystemProperty(named = REAL_L3_PROPERTY, matches = "true")
    void shouldAnswerBenchmarkQuestionsWithHitRate() throws IOException {
        Long userId = 1001L;
        // report id 对齐 dev 库（茅台 2025，L3 query_statement 按 reportId 读 dev 数据）
        Long reportId = saveReportWithId(userId, 17L);
        ChatSessionResponse session = chatService.createSession(userId,
                new CreateSessionRequest(reportId, "M5.09 基准验收")).block(Duration.ofSeconds(5));
        assertNotNull(session, "会话必须创建成功");

        Benchmark benchmark = loadBenchmark();
        assertEquals(5, benchmark.questions().size(), "基准应恰好 5 个标准问题");

        List<QaResult> results = new ArrayList<>();
        for (QaQuestion question : benchmark.questions()) {
            results.add(askOnce(session.id(), userId, question));
        }

        double hitRateMin = benchmark.hitRateMin();
        long firstTokenSlaMs = (long) (benchmark.firstTokenSecondsMax() * 1000);
        long hitCount = results.stream().filter(QaResult::hit).count();
        double hitRate = (double) hitCount / results.size();
        log.info("[M5.09] 命中率 {} / {} = {}（门槛 {}）",
                hitCount, results.size(), String.format("%.2f", hitRate), hitRateMin);
        results.forEach(r -> log.info("[M5.09] {} hit={} firstToken={}ms\n  问: {}\n  答: {}",
                r.question().id(), r.hit(), r.firstTokenMs(), r.question().question(), r.answer()));

        assertTrue(hitRate >= hitRateMin,
                String.format("命中率不达标：%.2f < %.2f（qa_questions.json acceptance）%n%s",
                        hitRate, hitRateMin, formatResults(results)));
        for (QaResult result : results) {
            assertTrue(result.firstTokenMs() < firstTokenSlaMs,
                    String.format("问题 %s 首 token %d ms ≥ %d ms（spec §12.1）",
                            result.question().id(), result.firstTokenMs(), firstTokenSlaMs));
        }
    }

    // ========================================================================
    // 私有工具
    // ========================================================================

    /**
     * 执行单问：发送 → 收集事件（计时首事件）→ 累计 token 文本 → 命中判定。
     *
     * @return 单问结果（问题/答案/首 token 延迟/命中标志）
     */
    private QaResult askOnce(Long sessionId, Long userId, QaQuestion question) {
        long start = System.nanoTime();
        AtomicReference<Long> firstEventMs = new AtomicReference<>();
        StringBuilder answer = new StringBuilder();
        List<ServerSentEvent<String>> events = chatService.sendMessage(sessionId, userId,
                new SendMessageRequest(question.question()))
                .doOnNext(event -> firstEventMs.compareAndSet(
                        null, Duration.ofNanos(System.nanoTime() - start).toMillis()))
                .doOnNext(event -> {
                    if ("token".equals(event.event())) {
                        answer.append(parseTokenContent(event.data()));
                    }
                })
                .collectList()
                .block(PER_QUESTION_TIMEOUT);
        assertNotNull(events, "问题 " + question.id() + " 必须返回事件流");

        String text = answer.toString();
        boolean hit = matchKeyFacts(text, question);
        return new QaResult(question, text, firstEventMs.get(), hit);
    }

    /**
     * 命中判定：答案（去空格/逗号）包含任一 key_fact（同规格化）；若基准声明了
     * direction（下降方向词）还需方向词一致；若声明了 forbid_words（否定表述）
     * 则命中但含否定词视为未命中（防止「不成立」被正向词子串误判为 HIT）。
     */
    private static boolean matchKeyFacts(String answer, QaQuestion question) {
        String normalized = answer.replaceAll("[\\s,，]", "");
        boolean numberHit = question.keyFacts().stream()
                .anyMatch(fact -> normalized.contains(fact.replaceAll("[\\s,，]", "")));
        if (!numberHit) {
            return false;
        }
        if (question.forbidWords() != null
                && question.forbidWords().stream().anyMatch(answer::contains)) {
            log.warn("[M5.09] {} 命中关键事实但含否定表述：{}", question.id(), answer);
            return false;
        }
        if (question.direction() == null || question.direction().isBlank()) {
            return true;
        }
        List<String> downWords = List.of("下降", "减少", "下滑", "负增长", "降低", "-");
        boolean directionOk = downWords.stream().anyMatch(answer::contains);
        if (!directionOk) {
            log.warn("[M5.09] {} 数值命中但方向表述异常：{}", question.id(), answer);
        }
        return directionOk;
    }

    /** 解析 assistant 消息 tools_used JSON 数组（与 ChatService.parseTools 同契约）。 */
    private static List<String> parseTools(String json) {
        if (json == null || json.isBlank()) {
            return List.of();
        }
        try {
            return new ObjectMapper().readValue(json, new TypeReference<List<String>>() {
            });
        } catch (Exception error) {
            return List.of();
        }
    }

    /** 解析 token 事件 data 的 content 字段（与 ChatService.TokenAccumulator 同契约）。 */
    private static String parseTokenContent(String data) {
        try {
            Map<String, Object> map = new ObjectMapper().readValue(
                    data, new TypeReference<Map<String, Object>>() {
                    });
            Object content = map.get("content");
            return content instanceof String text ? text : "";
        } catch (Exception error) {
            return "";
        }
    }

    /** 构造 L3 格式 SSE 事件（spec §6.3.3：event 名 + JSON data）。 */
    private static ServerSentEvent<String> sse(String event, String data) {
        return ServerSentEvent.<String>builder().event(event).data(data).build();
    }

    /** canned L3 事件流（与 L3 chat.py 渲染契约一致，供 mock 模式回归）。 */
    private static Flux<ServerSentEvent<String>> cannedEvents() {
        return Flux.just(
                sse("thought", "{\"step\":1,\"content\":\"分析：用户询问资产总计，需要查资产负债表\"}"),
                sse("tool_call",
                        "{\"step\":1,\"tool\":\"query_statement\",\"args\":\"{\\\"item\\\":\\\"资产总计\\\"}\"}"),
                sse("tool_result",
                        "{\"step\":1,\"tool\":\"query_statement\",\"result\":\"资产总计 303834844021.44 元\"}"),
                sse("token", "{\"content\":\"贵州茅台2025年末资产总计为\"}"),
                sse("token", "{\"content\":\"3038.35亿元。\"}"),
                sse("done", "{\"messageId\":\"9\",\"tokenCount\":2,"
                        + "\"toolsUsed\":[\"query_statement\"],\"finishedReason\":\"completed\"}"));
    }

    /** 建一条归属 userId 的 report（自增 id），返回 reportId。 */
    private Long saveReport(Long userId) {
        return saveReportWithId(userId, null);
    }

    /** 建 report；real 模式传指定 id（对齐 dev 库 report_id=17），mock 模式自增。 */
    private Long saveReportWithId(Long userId, Long id) {
        Report report = Report.builder()
                .id(id)
                .taskId("chat-it-" + UUID.randomUUID().toString().substring(0, 8))
                .userId(userId)
                .companyCode("600519")
                .companyName("贵州茅台")
                .reportType("ANNUAL")
                .reportPeriod("2025")
                .pdfMd5("chat_it_" + UUID.randomUUID().toString().replace("-", "").substring(0, 16))
                .pdfObjectKey("uploads/it/" + userId + "/chat.pdf")
                .pageCount(120)
                .parseStatus("COMPLETED")
                .build();
        if (id == null) {
            return reportRepository.save(report).map(Report::getId)
                    .block(Duration.ofSeconds(5));
        }
        // R2DBC save() 对带 id 实体走 UPDATE（Row with Id does not exist 报错），
        // real 模式必须对齐 dev 库 report_id=17，用原生 INSERT 显式指定主键。
        return databaseClient.sql("INSERT INTO report (id, task_id, user_id, company_code,"
                        + " company_name, report_type, report_period, pdf_md5, pdf_object_key,"
                        + " page_count, parse_status)"
                        + " VALUES (:id, :taskId, :userId, :companyCode, :companyName,"
                        + " :reportType, :reportPeriod, :pdfMd5, :pdfObjectKey, :pageCount, :parseStatus)")
                .bind("id", id)
                .bind("taskId", report.getTaskId())
                .bind("userId", report.getUserId())
                .bind("companyCode", report.getCompanyCode())
                .bind("companyName", report.getCompanyName())
                .bind("reportType", report.getReportType())
                .bind("reportPeriod", report.getReportPeriod())
                .bind("pdfMd5", report.getPdfMd5())
                .bind("pdfObjectKey", report.getPdfObjectKey())
                .bind("pageCount", report.getPageCount())
                .bind("parseStatus", report.getParseStatus())
                .then()
                .thenReturn(id)
                .block(Duration.ofSeconds(5));
    }

    /** 读取并解析 benchmark 问题文件（questions + acceptance 验收口径）。 */
    @SuppressWarnings("unchecked")
    private static Benchmark loadBenchmark() throws IOException {
        String path = System.getProperty(BENCHMARK_PATH_PROPERTY, DEFAULT_BENCHMARK_PATH);
        java.nio.file.Path file = java.nio.file.Path.of(path).toAbsolutePath().normalize();
        ObjectMapper mapper = new ObjectMapper();
        Map<String, Object> root = mapper.readValue(file.toFile(),
                new TypeReference<Map<String, Object>>() {
                });
        Map<String, Object> acceptance = (Map<String, Object>) root.get("acceptance");
        double hitRateMin = ((Number) acceptance.get("hit_rate_min")).doubleValue();
        double firstTokenSecondsMax = ((Number) acceptance.get("first_token_seconds_max")).doubleValue();
        List<Map<String, Object>> raw = (List<Map<String, Object>>) root.get("questions");
        List<QaQuestion> questions = raw.stream().map(m -> new QaQuestion(
                String.valueOf(m.get("id")),
                String.valueOf(m.get("question")),
                (List<String>) m.get("key_facts"),
                (String) m.get("direction"),
                (List<String>) m.getOrDefault("forbid_words", List.of()))).toList();
        return new Benchmark(questions, hitRateMin, firstTokenSecondsMax);
    }

    /** 格式化逐问明细（失败信息用）。 */
    private static String formatResults(List<QaResult> results) {
        StringBuilder sb = new StringBuilder();
        results.forEach(r -> sb.append("  [").append(r.hit() ? "HIT" : "MISS").append("] ")
                .append(r.question().id()).append(" (首 token ")
                .append(r.firstTokenMs()).append(" ms): ")
                .append(r.question().question()).append(" → ")
                .append(r.answer()).append('\n'));
        return sb.toString();
    }

    /** 基准问题（data/benchmark/qa_questions.json 单条）。 */
    private record QaQuestion(
            String id, String question, List<String> keyFacts, String direction,
            List<String> forbidWords) {
    }

    /** 基准文件（qa_questions.json：questions + acceptance 验收口径）。 */
    private record Benchmark(List<QaQuestion> questions, double hitRateMin, double firstTokenSecondsMax) {
    }

    /** 单问执行结果。 */
    private record QaResult(QaQuestion question, String answer, Long firstTokenMs, boolean hit) {
    }
}
