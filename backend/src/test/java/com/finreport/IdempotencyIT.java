package com.finreport;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

import java.time.Duration;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;

import org.awaitility.Awaitility;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.reactive.server.WebTestClient;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.MySQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.utility.DockerImageName;

import reactor.core.publisher.Mono;

/**
 * M6.04 幂等集成测试 — 真实 Redis + MySQL（Testcontainers）。
 *
 * <p>验收：重复请求返回原结果（同 sessionId + Idempotency-Replayed 头）；
 * 写操作缺失 Idempotency-Key 返回 422。走完整 HTTP 过滤器链
 * （JwtFilter → RateLimit → Idempotent），命名沿用 {@code *IT.java} 约定。</p>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("it")
@Testcontainers
class IdempotencyIT {

    private static final String USER = "finreport";
    private static final String PASSWORD = "finreport";
    private static final String DATABASE = "finreport";

    @Container
    private static final MySQLContainer<?> MYSQL = new MySQLContainer<>(DockerImageName.parse("mysql:8.0.36"))
            .withDatabaseName(DATABASE)
            .withUsername(USER)
            .withPassword(PASSWORD);

    @Container
    private static final GenericContainer<?> REDIS = new GenericContainer<>(DockerImageName.parse("redis:7.2-alpine"))
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
    private WebTestClient webTestClient;

    @Autowired
    private org.springframework.r2dbc.core.DatabaseClient databaseClient;

    @Test
    @DisplayName("should replay original response for duplicated idempotency key")
    void shouldReplayOriginalResponseForDuplicatedIdempotencyKey() {
        String username = "idem_" + UUID.randomUUID().toString().substring(0, 8);
        AtomicReference<Long> userIdRef = new AtomicReference<>();

        // 1. 注册拿 token（注册端点有限流 5 次/分/IP，本用例仅 1 次）
        String accessToken = webTestClient.post().uri("/api/v1/auth/register")
                .bodyValue(Map.of("username", username, "password", "Passw0rd!123"))
                .exchange()
                .expectStatus().isCreated()
                .expectBody(TokenBody.class)
                .returnResult().getResponseBody().accessToken();
        assertNotNull(accessToken);

        // 2. 查 userId 并插入归属 report
        Awaitility.await().atMost(Duration.ofSeconds(10)).until(() ->
                databaseClient.sql("SELECT id FROM user_account WHERE username = :u")
                        .bind("u", username)
                        .map((row, meta) -> row.get("id", Long.class))
                        .one()
                        .doOnNext(userIdRef::set)
                        .map(id -> true)
                        .defaultIfEmpty(false)
                        .block());
        Long userId = userIdRef.get();
        long reportId = 9001L;
        insertReport(reportId, userId);

        // 3. 首次创建会话（带 Idempotency-Key）
        String idempotencyKey = "it-key-" + UUID.randomUUID();
        Map<String, Object> body = Map.of("reportId", reportId);
        Long firstSessionId = postSession(accessToken, idempotencyKey, body, false);
        assertNotNull(firstSessionId);

        // 4. 同 Key 重复请求 → 回放原响应（相同 sessionId + Replayed 头）
        Long replayedSessionId = postSession(accessToken, idempotencyKey, body, true);
        assertEquals(firstSessionId, replayedSessionId);
    }

    @Test
    @DisplayName("should return 422 when idempotent write lacks idempotency key")
    void shouldReturn422WhenIdempotentWriteLacksIdempotencyKey() {
        String username = "idem_" + UUID.randomUUID().toString().substring(0, 8);
        String accessToken = webTestClient.post().uri("/api/v1/auth/register")
                .bodyValue(Map.of("username", username, "password", "Passw0rd!123"))
                .exchange()
                .expectStatus().isCreated()
                .expectBody(TokenBody.class)
                .returnResult().getResponseBody().accessToken();

        webTestClient.post().uri("/api/v1/chat/sessions")
                .header("Authorization", "Bearer " + accessToken)
                .bodyValue(Map.of("reportId", 1))
                .exchange()
                .expectStatus().isEqualTo(422);
    }

    // --- helpers ---

    private Long postSession(String accessToken, String idempotencyKey, Map<String, Object> body,
                             boolean expectReplayed) {
        var responseSpec = webTestClient.post().uri("/api/v1/chat/sessions")
                .header("Authorization", "Bearer " + accessToken)
                .header("Idempotency-Key", idempotencyKey)
                .bodyValue(body)
                .exchange()
                .expectStatus().isOk();
        if (expectReplayed) {
            responseSpec.expectHeader().valueEquals("Idempotency-Replayed", "true");
        }
        return responseSpec.expectBody(SessionBody.class)
                .returnResult().getResponseBody().id();
    }

    private void insertReport(long id, long userId) {
        databaseClient.sql("INSERT INTO report (id, task_id, user_id, company_code,"
                        + " company_name, report_type, report_period, pdf_md5, pdf_object_key,"
                        + " page_count, parse_status)"
                        + " VALUES (:id, :taskId, :userId, :companyCode, :companyName,"
                        + " :reportType, :reportPeriod, :pdfMd5, :pdfObjectKey, :pageCount, :parseStatus)")
                .bind("id", id)
                .bind("taskId", "task-it-" + id)
                .bind("userId", userId)
                .bind("companyCode", "600519")
                .bind("companyName", "集成测试公司")
                .bind("reportType", "ANNUAL")
                .bind("reportPeriod", "2025")
                .bind("pdfMd5", "md5-" + id)
                .bind("pdfObjectKey", "it/" + id + ".pdf")
                .bind("pageCount", 10)
                .bind("parseStatus", "COMPLETED")
                .then()
                .block(Duration.ofSeconds(10));
    }

    /** 注册响应体（只取 accessToken）。 */
    record TokenBody(String accessToken) {
    }

    /** 会话响应体（只取 id）。 */
    record SessionBody(Long id) {
    }
}
