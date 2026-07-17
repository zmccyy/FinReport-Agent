package com.finreport;

import static org.junit.jupiter.api.Assertions.*;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * Flyway 迁移 SQL 结构验证。
 *
 * <p>验证所有迁移文件存在、命名规范正确、表结构与 spec §5.2.2 一致。
 * 完整集成测试（Flyway 实际执行）需要 MySQL 容器，在 CI/CD 中运行。</p>
 */
class FlywayMigrationTest {

    private static final String MIGRATION_DIR = "db/migration/";
    private static final Pattern CREATE_TABLE = Pattern.compile(
            "CREATE\\s+TABLE\\s+(\\w+)", Pattern.CASE_INSENSITIVE);
    private static final Pattern COLUMN_DEF = Pattern.compile(
            "^\\s{2}(\\w+)\\s+", Pattern.MULTILINE);

    @Test
    @DisplayName("所有 6 个迁移文件存在")
    void shouldHaveSixMigrationFiles() {
        List<String> files = listMigrationFiles();
        assertEquals(6, files.size(), "应有 6 个迁移文件");
        assertTrue(files.contains("V1__init_user.sql"), "缺少 V1__init_user.sql");
        assertTrue(files.contains("V2__init_report.sql"), "缺少 V2__init_report.sql");
        assertTrue(files.contains("V3__init_task.sql"), "缺少 V3__init_task.sql");
        assertTrue(files.contains("V4__init_model_audit.sql"), "缺少 V4__init_model_audit.sql");
        assertTrue(files.contains("V5__init_indexes.sql"), "缺少 V5__init_indexes.sql");
        assertTrue(files.contains("V6__m1_reliability_hardening.sql"),
                "缺少 V6__m1_reliability_hardening.sql");
    }

    @Test
    @DisplayName("总共 12 张表")
    void shouldHaveTwelveTables() {
        List<String> allTables = new ArrayList<>();
        for (String file : listMigrationFiles()) {
            String content = readMigrationFile(file);
            Matcher m = CREATE_TABLE.matcher(content);
            while (m.find()) {
                allTables.add(m.group(1));
            }
        }
        assertEquals(12, allTables.size(),
                "应有 12 张表，实际: " + String.join(", ", allTables));

        // 验证所有预期表名
        List<String> expected = List.of(
                "user_account",
                "report", "financial_statement", "accounting_check", "anomaly", "report_artifact",
                "task", "task_step", "chat_session", "chat_message",
                "model_registry", "audit_log");
        for (String table : expected) {
            assertTrue(allTables.contains(table), "缺少表: " + table);
        }
    }

    @Test
    @DisplayName("V1 用户域 — user_account 表结构")
    void v1_userAccountColumns() {
        List<String> cols = extractColumns("V1__init_user.sql");
        assertTrue(cols.contains("id"), "缺少 id");
        assertTrue(cols.contains("username"), "缺少 username");
        assertTrue(cols.contains("password_hash"), "缺少 password_hash");
        assertTrue(cols.contains("email"), "缺少 email");
        assertTrue(cols.contains("role"), "缺少 role");
        assertTrue(cols.contains("status"), "缺少 status");
        assertTrue(cols.contains("created_at"), "缺少 created_at");
        assertTrue(cols.contains("updated_at"), "缺少 updated_at");
    }

    @Test
    @DisplayName("V2 财报域 — 5 张表")
    void v2_reportDomainTables() {
        String content = readMigrationFile("V2__init_report.sql");
        Matcher m = CREATE_TABLE.matcher(content);
        List<String> tables = new ArrayList<>();
        while (m.find()) tables.add(m.group(1));
        assertEquals(5, tables.size(), "V2 应有 5 张表");
        assertTrue(tables.contains("report"));
        assertTrue(tables.contains("financial_statement"));
        assertTrue(tables.contains("accounting_check"));
        assertTrue(tables.contains("anomaly"));
        assertTrue(tables.contains("report_artifact"));
    }

    @Test
    @DisplayName("V2 report 表关键字段")
    void v2_reportColumns() {
        List<String> cols = extractColumns("V2__init_report.sql", "report");
        for (String c : List.of("id", "task_id", "user_id", "company_code",
                "company_name", "report_type", "report_period", "pdf_md5",
                "pdf_object_key", "page_count", "parse_status", "created_at")) {
            assertTrue(cols.contains(c), "report 缺少字段: " + c);
        }
    }

    @Test
    @DisplayName("V2 financial_statement 表关键字段")
    void v2_financialStatementColumns() {
        List<String> cols = extractColumns("V2__init_report.sql", "financial_statement");
        for (String c : List.of("id", "report_id", "statement_type", "item_name",
                "item_value", "currency", "unit", "scope", "period_type",
                "confidence", "source_page", "source_bbox", "created_at")) {
            assertTrue(cols.contains(c), "financial_statement 缺少字段: " + c);
        }
    }

    @Test
    @DisplayName("V2 accounting_check 表关键字段")
    void v2_accountingCheckColumns() {
        List<String> cols = extractColumns("V2__init_report.sql", "accounting_check");
        for (String c : List.of("id", "report_id", "rule_name", "rule_type",
                "expected", "actual", "diff", "is_pass", "severity", "note", "created_at")) {
            assertTrue(cols.contains(c), "accounting_check 缺少字段: " + c);
        }
    }

    @Test
    @DisplayName("V2 anomaly 表关键字段")
    void v2_anomalyColumns() {
        List<String> cols = extractColumns("V2__init_report.sql", "anomaly");
        for (String c : List.of("id", "report_id", "item_name", "anomaly_type",
                "metric_value", "threshold", "description", "severity", "created_at")) {
            assertTrue(cols.contains(c), "anomaly 缺少字段: " + c);
        }
    }

    @Test
    @DisplayName("V2 report_artifact 表关键字段")
    void v2_reportArtifactColumns() {
        List<String> cols = extractColumns("V2__init_report.sql", "report_artifact");
        for (String c : List.of("id", "report_id", "artifact_type",
                "object_key", "status", "created_at")) {
            assertTrue(cols.contains(c), "report_artifact 缺少字段: " + c);
        }
    }

    @Test
    @DisplayName("V3 任务域 — 4 张表")
    void v3_taskDomainTables() {
        String content = readMigrationFile("V3__init_task.sql");
        Matcher m = CREATE_TABLE.matcher(content);
        List<String> tables = new ArrayList<>();
        while (m.find()) tables.add(m.group(1));
        assertEquals(4, tables.size(), "V3 应有 4 张表");
        assertTrue(tables.contains("task"));
        assertTrue(tables.contains("task_step"));
        assertTrue(tables.contains("chat_session"));
        assertTrue(tables.contains("chat_message"));
    }

    @Test
    @DisplayName("V3 task 表关键字段")
    void v3_taskColumns() {
        List<String> cols = extractColumns("V3__init_task.sql", "task");
        for (String c : List.of("id", "user_id", "task_type", "ref_report_id",
                "status", "current_step", "progress", "payload", "result",
                "error_msg", "started_at", "finished_at", "created_at")) {
            assertTrue(cols.contains(c), "task 缺少字段: " + c);
        }
    }

    @Test
    @DisplayName("V3 task_step 表关键字段")
    void v3_taskStepColumns() {
        List<String> cols = extractColumns("V3__init_task.sql", "task_step");
        for (String c : List.of("id", "task_id", "step_name", "status",
                "started_at", "finished_at", "duration_ms", "message_id", "error_msg")) {
            assertTrue(cols.contains(c), "task_step 缺少字段: " + c);
        }
    }

    @Test
    @DisplayName("V3 chat_session 表关键字段")
    void v3_chatSessionColumns() {
        List<String> cols = extractColumns("V3__init_task.sql", "chat_session");
        for (String c : List.of("id", "user_id", "report_id", "title",
                "created_at", "updated_at")) {
            assertTrue(cols.contains(c), "chat_session 缺少字段: " + c);
        }
    }

    @Test
    @DisplayName("V3 chat_message 表关键字段")
    void v3_chatMessageColumns() {
        List<String> cols = extractColumns("V3__init_task.sql", "chat_message");
        for (String c : List.of("id", "session_id", "role", "content",
                "tools_used", "token_count", "created_at")) {
            assertTrue(cols.contains(c), "chat_message 缺少字段: " + c);
        }
    }

    @Test
    @DisplayName("V4 模型与审计域 — 2 张表")
    void v4_modelAuditDomainTables() {
        String content = readMigrationFile("V4__init_model_audit.sql");
        Matcher m = CREATE_TABLE.matcher(content);
        List<String> tables = new ArrayList<>();
        while (m.find()) tables.add(m.group(1));
        assertEquals(2, tables.size(), "V4 应有 2 张表");
        assertTrue(tables.contains("model_registry"));
        assertTrue(tables.contains("audit_log"));
    }

    @Test
    @DisplayName("V4 model_registry 表关键字段")
    void v4_modelRegistryColumns() {
        List<String> cols = extractColumns("V4__init_model_audit.sql", "model_registry");
        for (String c : List.of("id", "task", "base_model", "adapter_path",
                "version", "metrics", "status", "data_version", "train_cmd_hash", "trained_at")) {
            assertTrue(cols.contains(c), "model_registry 缺少字段: " + c);
        }
    }

    @Test
    @DisplayName("V4 audit_log 表关键字段")
    void v4_auditLogColumns() {
        List<String> cols = extractColumns("V4__init_model_audit.sql", "audit_log");
        for (String c : List.of("id", "user_id", "action", "target",
                "ip", "user_agent", "payload", "created_at")) {
            assertTrue(cols.contains(c), "audit_log 缺少字段: " + c);
        }
    }

    @Test
    @DisplayName("V5 包含 10 个额外索引")
    void v5_indexCount() {
        String content = readMigrationFile("V5__init_indexes.sql");
        Pattern createIndex = Pattern.compile("CREATE\\s+INDEX\\s+(\\w+)", Pattern.CASE_INSENSITIVE);
        Matcher m = createIndex.matcher(content);
        List<String> indexes = new ArrayList<>();
        while (m.find()) indexes.add(m.group(1));
        assertEquals(10, indexes.size(),
                "V5 应有 10 个额外索引，实际: " + String.join(", ", indexes));
    }

    @Test
    @DisplayName("V6 可靠性加固迁移包含去重、重试和任务索引")
    void v6_reliabilityHardening() {
        String content = readMigrationFile("V6__m1_reliability_hardening.sql");
        assertTrue(content.contains("DROP INDEX uk_md5"), "V6 必须移除跨用户 uk_md5 约束");
        assertTrue(content.contains("DROP INDEX task_id"), "V6 必须解除 report.task_id 单值约束");
        assertTrue(content.contains("uk_report_user_md5 (user_id, pdf_md5)"),
                "V6 必须创建用户隔离的 MD5 唯一索引");
        assertTrue(content.contains("idx_report_task (task_id)"),
                "V6 必须保留 report.task_id 查询索引");
        assertTrue(content.contains("ADD COLUMN retry_count"),
                "V6 必须记录 task_step 重试次数");
        assertTrue(content.contains("idx_task_step_idempotency (task_id, step_name, status)"),
                "V6 必须创建任务步骤幂等查询索引");
    }

    @Test
    @DisplayName("所有表使用 InnoDB + utf8mb4")
    void shouldUseInnoDBAndUtf8mb4() {
        for (String file : listMigrationFiles()) {
            String content = readMigrationFile(file);
            Matcher createMatcher = CREATE_TABLE.matcher(content);
            while (createMatcher.find()) {
                String tableName = createMatcher.group(1);
                // Find the full CREATE TABLE block
                int start = createMatcher.start();
                int end = content.indexOf(";", start) + 1;
                if (end > start) {
                    String block = content.substring(start, end);
                    assertTrue(
                            block.toUpperCase().contains("ENGINE=INNODB"),
                            tableName + " 应使用 InnoDB");
                    assertTrue(
                            block.toUpperCase().contains("CHARSET=UTF8MB4"),
                            tableName + " 应使用 utf8mb4");
                }
            }
        }
    }

    @Test
    @DisplayName("表级时间戳字段与 spec §5.2.2 一致")
    void shouldHaveTimestampPerSpec() {
        // 按 spec §5.2.2：model_registry 用 trained_at，task_step 无独立时间戳
        // （通过 task_id 关联 task.created_at），其余表均含 created_at
        var noCreatedAt = List.of("model_registry", "task_step");

        for (String file : listMigrationFiles()) {
            String content = readMigrationFile(file);
            Matcher m = CREATE_TABLE.matcher(content);
            while (m.find()) {
                String tableName = m.group(1);
                if (noCreatedAt.contains(tableName)) continue;
                int start = m.start();
                int end = content.indexOf(";", start) + 1;
                if (end > start) {
                    String block = content.substring(start, end);
                    assertTrue(block.contains("created_at"),
                            tableName + " 应有 created_at 字段");
                }
            }
        }
    }

    // ========== 辅助方法 ==========

    private List<String> listMigrationFiles() {
        List<String> files = new ArrayList<>();
        try (InputStream is = getClass().getClassLoader()
                .getResourceAsStream(MIGRATION_DIR);
             BufferedReader reader = new BufferedReader(
                     new InputStreamReader(is, StandardCharsets.UTF_8))) {
            // The directory listing via classpath doesn't work directly.
            // Use known list.
        } catch (Exception e) {
            // fallback: manually list
        }

        // Classpath resource listing doesn't work reliably for directories.
        // Instead, check each known file exists.
        String[] known = {"V1__init_user.sql", "V2__init_report.sql",
                "V3__init_task.sql", "V4__init_model_audit.sql", "V5__init_indexes.sql",
                "V6__m1_reliability_hardening.sql"};
        for (String name : known) {
            String path = MIGRATION_DIR + name;
            if (getClass().getClassLoader().getResource(path) != null) {
                files.add(name);
            }
        }
        return files;
    }

    private String readMigrationFile(String fileName) {
        String path = MIGRATION_DIR + fileName;
        try (InputStream is = getClass().getClassLoader().getResourceAsStream(path)) {
            assertNotNull(is, "找不到迁移文件: " + path);
            return new String(is.readAllBytes(), StandardCharsets.UTF_8);
        } catch (Exception e) {
            throw new RuntimeException("读取迁移文件失败: " + fileName, e);
        }
    }

    /**
     * 从指定迁移文件中提取所有列名。
     */
    private List<String> extractColumns(String fileName) {
        return extractColumns(fileName, null);
    }

    /**
     * 从指定表定义中提取列名。tableName 为 null 时提取文件中所有列。
     */
    private List<String> extractColumns(String fileName, String tableName) {
        String content = readMigrationFile(fileName);
        List<String> columns = new ArrayList<>();

        if (tableName != null) {
            // 定位到指定表
            Pattern p = Pattern.compile(
                    "CREATE\\s+TABLE\\s+" + tableName + "\\b", Pattern.CASE_INSENSITIVE);
            Matcher m = p.matcher(content);
            if (!m.find()) return columns;
            int start = m.start();
            int end = content.indexOf(";", start) + 1;
            if (end <= start) return columns;
            content = content.substring(start, end);
        }

        // 提取列定义行（2空格缩进开头）
        for (String line : content.split("\n")) {
            Matcher colMatch = COLUMN_DEF.matcher(line);
            if (colMatch.find()) {
                String colName = colMatch.group(1).toLowerCase();
                // 跳过 SQL 关键字
                if (List.of("primary", "unique", "index", "key", "constraint",
                        "foreign", "check").contains(colName)) continue;
                columns.add(colName);
            }
        }
        return columns;
    }
}
