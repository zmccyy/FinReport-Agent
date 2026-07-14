package com.finreport;

import static org.junit.jupiter.api.Assertions.*;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;

import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;

/**
 * Flyway 迁移集成测试 — 连接真实 MySQL 执行迁移并验证。
 *
 * <p>需要 Docker MySQL 容器运行中：docker compose -f deploy/docker-compose.yml up -d mysql</p>
 */
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class FlywayIntegrationTest {

    private static final String JDBC_URL = "jdbc:mysql://localhost:3306/finreport?useSSL=false&allowPublicKeyRetrieval=true";
    private static final String USER = "finreport";
    private static final String PASSWORD = "finreport";

    @Test
    @Order(1)
    @DisplayName("Flyway migrate — 执行 5 个迁移文件，创建 12 张表")
    void shouldMigrateAllMigrations() {
        Flyway flyway = Flyway.configure()
                .dataSource(JDBC_URL, USER, PASSWORD)
                .locations("classpath:db/migration")
                .baselineOnMigrate(true)
                .baselineVersion("0")
                .cleanDisabled(false)  // 测试用：允许清理数据库
                .load();

        // 先清理以便重复测试
        flyway.clean();

        // 执行迁移
        int applied = flyway.migrate().migrationsExecuted;
        assertEquals(5, applied, "应执行 5 个迁移文件");
    }

    @Test
    @Order(2)
    @DisplayName("验证 — 12 张业务表全部存在（不含 flyway_schema_history）")
    void shouldHaveAll12Tables() throws Exception {
        List<String> tables = new ArrayList<>(listTables());
        tables.remove("flyway_schema_history");  // Flyway 自带版本追踪表
        assertEquals(12, tables.size(),
                "应有 12 张业务表，实际 " + tables.size() + ": " + String.join(", ", tables));

        List<String> expected = List.of(
                "user_account",
                "report", "financial_statement", "accounting_check", "anomaly", "report_artifact",
                "task", "task_step", "chat_session", "chat_message",
                "model_registry", "audit_log");

        for (String table : expected) {
            assertTrue(tables.contains(table), "缺少表: " + table);
        }
    }

    @Test
    @Order(3)
    @DisplayName("验证 — user_account 字段匹配 spec §5.2.2")
    void shouldHaveCorrectUserAccountColumns() throws Exception {
        List<String> cols = listColumns("user_account");
        for (String c : List.of("id", "username", "password_hash", "email",
                "role", "status", "created_at", "updated_at")) {
            assertTrue(cols.contains(c), "user_account 缺少字段: " + c);
        }
    }

    @Test
    @Order(4)
    @DisplayName("验证 — report 字段匹配 spec §5.2.2")
    void shouldHaveCorrectReportColumns() throws Exception {
        List<String> cols = listColumns("report");
        for (String c : List.of("id", "task_id", "user_id", "company_code",
                "company_name", "report_type", "report_period", "pdf_md5",
                "pdf_object_key", "page_count", "parse_status", "created_at")) {
            assertTrue(cols.contains(c), "report 缺少字段: " + c);
        }
    }

    @Test
    @Order(5)
    @DisplayName("验证 — financial_statement 字段匹配 spec §5.2.2")
    void shouldHaveCorrectFinancialStatementColumns() throws Exception {
        List<String> cols = listColumns("financial_statement");
        for (String c : List.of("id", "report_id", "statement_type", "item_name",
                "item_value", "currency", "unit", "scope", "period_type",
                "confidence", "source_page", "source_bbox", "created_at")) {
            assertTrue(cols.contains(c), "financial_statement 缺少字段: " + c);
        }
    }

    @Test
    @Order(6)
    @DisplayName("验证 — accounting_check 字段匹配 spec §5.2.2")
    void shouldHaveCorrectAccountingCheckColumns() throws Exception {
        List<String> cols = listColumns("accounting_check");
        for (String c : List.of("id", "report_id", "rule_name", "rule_type",
                "expected", "actual", "diff", "is_pass", "severity", "note", "created_at")) {
            assertTrue(cols.contains(c), "accounting_check 缺少字段: " + c);
        }
    }

    @Test
    @Order(7)
    @DisplayName("验证 — anomaly 字段匹配 spec §5.2.2")
    void shouldHaveCorrectAnomalyColumns() throws Exception {
        List<String> cols = listColumns("anomaly");
        for (String c : List.of("id", "report_id", "item_name", "anomaly_type",
                "metric_value", "threshold", "description", "severity", "created_at")) {
            assertTrue(cols.contains(c), "anomaly 缺少字段: " + c);
        }
    }

    @Test
    @Order(8)
    @DisplayName("验证 — report_artifact 字段匹配 spec §5.2.2")
    void shouldHaveCorrectReportArtifactColumns() throws Exception {
        List<String> cols = listColumns("report_artifact");
        for (String c : List.of("id", "report_id", "artifact_type",
                "object_key", "status", "created_at")) {
            assertTrue(cols.contains(c), "report_artifact 缺少字段: " + c);
        }
    }

    @Test
    @Order(9)
    @DisplayName("验证 — task 字段匹配 spec §5.2.2")
    void shouldHaveCorrectTaskColumns() throws Exception {
        List<String> cols = listColumns("task");
        for (String c : List.of("id", "user_id", "task_type", "ref_report_id",
                "status", "current_step", "progress", "payload", "result",
                "error_msg", "started_at", "finished_at", "created_at")) {
            assertTrue(cols.contains(c), "task 缺少字段: " + c);
        }
    }

    @Test
    @Order(10)
    @DisplayName("验证 — task_step 字段匹配 spec §5.2.2")
    void shouldHaveCorrectTaskStepColumns() throws Exception {
        List<String> cols = listColumns("task_step");
        for (String c : List.of("id", "task_id", "step_name", "status",
                "started_at", "finished_at", "duration_ms", "message_id", "error_msg")) {
            assertTrue(cols.contains(c), "task_step 缺少字段: " + c);
        }
    }

    @Test
    @Order(11)
    @DisplayName("验证 — chat_session 字段匹配 spec §5.2.2")
    void shouldHaveCorrectChatSessionColumns() throws Exception {
        List<String> cols = listColumns("chat_session");
        for (String c : List.of("id", "user_id", "report_id", "title",
                "created_at", "updated_at")) {
            assertTrue(cols.contains(c), "chat_session 缺少字段: " + c);
        }
    }

    @Test
    @Order(12)
    @DisplayName("验证 — chat_message 字段匹配 spec §5.2.2")
    void shouldHaveCorrectChatMessageColumns() throws Exception {
        List<String> cols = listColumns("chat_message");
        for (String c : List.of("id", "session_id", "role", "content",
                "tools_used", "token_count", "created_at")) {
            assertTrue(cols.contains(c), "chat_message 缺少字段: " + c);
        }
    }

    @Test
    @Order(13)
    @DisplayName("验证 — model_registry 字段匹配 spec §5.2.2")
    void shouldHaveCorrectModelRegistryColumns() throws Exception {
        List<String> cols = listColumns("model_registry");
        for (String c : List.of("id", "task", "base_model", "adapter_path",
                "version", "metrics", "status", "data_version", "train_cmd_hash", "trained_at")) {
            assertTrue(cols.contains(c), "model_registry 缺少字段: " + c);
        }
    }

    @Test
    @Order(14)
    @DisplayName("验证 — audit_log 字段匹配 spec §5.2.2")
    void shouldHaveCorrectAuditLogColumns() throws Exception {
        List<String> cols = listColumns("audit_log");
        for (String c : List.of("id", "user_id", "action", "target",
                "ip", "user_agent", "payload", "created_at")) {
            assertTrue(cols.contains(c), "audit_log 缺少字段: " + c);
        }
    }

    @Test
    @Order(15)
    @DisplayName("验证 — report 表索引（含 V5 额外索引）")
    void shouldHaveReportIndexes() throws Exception {
        List<String> idx = listIndexes("report");
        assertTrue(idx.contains("uk_md5"), "缺少唯一索引 uk_md5");
        assertTrue(idx.contains("idx_user_period"), "缺少 idx_user_period");
        assertTrue(idx.contains("idx_company"), "缺少 idx_company");
        assertTrue(idx.contains("idx_report_parse_status"), "缺少 V5 索引 idx_report_parse_status");
        assertTrue(idx.contains("idx_report_user_id"), "缺少 V5 索引 idx_report_user_id");
    }

    @Test
    @Order(16)
    @DisplayName("验证 — V5 额外索引全部存在")
    void shouldHaveAllV5Indexes() throws Exception {
        List<String> allIndexes = new ArrayList<>();
        for (String table : List.of("report", "accounting_check", "anomaly",
                "report_artifact", "task", "task_step", "chat_session", "model_registry")) {
            allIndexes.addAll(listIndexes(table));
        }
        for (String idx : List.of(
                "idx_report_parse_status", "idx_report_user_id",
                "idx_check_rule_type", "idx_anomaly_type_severity",
                "idx_anomaly_severity", "idx_artifact_type_status",
                "idx_task_ref_report", "idx_task_step_status",
                "idx_session_report", "idx_model_task")) {
            assertTrue(allIndexes.contains(idx), "缺少 V5 索引: " + idx);
        }
    }

    // ========== 辅助方法 ==========

    private List<String> listTables() throws Exception {
        List<String> tables = new ArrayList<>();
        try (Connection conn = DriverManager.getConnection(JDBC_URL, USER, PASSWORD);
             ResultSet rs = conn.getMetaData().getTables(
                     "finreport", null, "%", new String[]{"TABLE"})) {
            while (rs.next()) {
                tables.add(rs.getString("TABLE_NAME"));
            }
        }
        return tables;
    }

    private List<String> listColumns(String tableName) throws Exception {
        List<String> cols = new ArrayList<>();
        try (Connection conn = DriverManager.getConnection(JDBC_URL, USER, PASSWORD);
             ResultSet rs = conn.getMetaData().getColumns(
                     "finreport", null, tableName, null)) {
            while (rs.next()) {
                cols.add(rs.getString("COLUMN_NAME"));
            }
        }
        return cols;
    }

    private List<String> listIndexes(String tableName) throws Exception {
        List<String> indexes = new ArrayList<>();
        try (Connection conn = DriverManager.getConnection(JDBC_URL, USER, PASSWORD);
             ResultSet rs = conn.getMetaData().getIndexInfo(
                     "finreport", null, tableName, false, false)) {
            while (rs.next()) {
                String idxName = rs.getString("INDEX_NAME");
                if (idxName != null) {
                    indexes.add(idxName);
                }
            }
        }
        return indexes;
    }
}
