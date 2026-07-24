package com.finreport;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;

import org.flywaydb.core.Flyway;
import org.flywaydb.core.api.output.MigrateResult;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.MySQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.utility.DockerImageName;

/**
 * Flyway 迁移集成测试：在 Testcontainers MySQL 中执行全新建库及 V7 到 V8 升级。
 *
 * <p>使用 {@link MethodOrderer.MethodName} 保证测试方法按字母序执行：
 * {@code shouldEnforceUserScopedDedupe...}（V6，先插入 PDF_MD5 + userId=1）
 * 必须在 {@code shouldExpandRuleTypeToVarchar32...}（V7，复用同一条 report）
 * 之前运行，避免唯一约束 uk_report_user_md5 冲突。</p>
 */
@Testcontainers
@TestMethodOrder(MethodOrderer.MethodName.class)
class FlywayMigrationIT {

    private static final String USER = "finreport";
    private static final String PASSWORD = "finreport";
    private static final String FRESH_DATABASE = "finreport";
    private static final String UPGRADE_DATABASE = "finreport_upgrade";
    private static final String MYSQL_IMAGE = "mysql:8.0.36";
    private static final String PDF_MD5 = "0123456789abcdef0123456789abcdef";

    @Container
    private static final MySQLContainer<?> FRESH_MYSQL = mysqlContainer(FRESH_DATABASE);

    @Container
    private static final MySQLContainer<?> UPGRADE_MYSQL = mysqlContainer(UPGRADE_DATABASE);

    @BeforeAll
    static void migrateFreshAndUpgradeSchemas() throws SQLException {
        assertEquals(8, migrate(freshJdbcUrl()).migrationsExecuted,
                "全新数据库应执行 V1 到 V8 共 8 个迁移");

        assertEquals(7, migrateToVersion(upgradeJdbcUrl(), "7").migrationsExecuted,
                "既有库应先执行 V1 到 V7");
        assertEquals(1, migrate(upgradeJdbcUrl()).migrationsExecuted,
                "既有 V7 库升级时应只执行 V8");
    }

    @Test
    @DisplayName("全新库的迁移历史包含 V1 到 V8")
    void shouldMigrateAllVersionsOnFreshDatabase() throws SQLException {
        assertEquals(8, countAppliedMigrations(freshJdbcUrl()), "应有 8 条成功迁移历史");
    }

    @Test
    @DisplayName("全新库包含 12 张 M1 业务表")
    void shouldCreateAllM1Tables() throws SQLException {
        List<String> tables = new ArrayList<>(listTables(freshJdbcUrl(), FRESH_DATABASE));
        tables.remove("flyway_schema_history");
        assertEquals(12, tables.size(), "应有 12 张业务表");
        for (String table : List.of(
                "user_account", "report", "financial_statement", "accounting_check", "anomaly",
                "report_artifact", "task", "task_step", "chat_session", "chat_message",
                "model_registry", "audit_log")) {
            assertTrue(tables.contains(table), "缺少表: " + table);
        }
    }

    @Test
    @DisplayName("V6 在全新库和既有 V5 库中均加入 retry_count 与可靠性索引")
    void shouldApplyReliabilitySchemaToFreshAndUpgradedDatabases() throws SQLException {
        for (String jdbcUrl : List.of(freshJdbcUrl(), upgradeJdbcUrl())) {
            assertTrue(listColumns(jdbcUrl, "task_step").contains("retry_count"),
                    "task_step 应包含 retry_count");
            List<String> reportIndexes = listIndexes(jdbcUrl, "report");
            assertTrue(reportIndexes.contains("uk_report_user_md5"),
                    "report 应包含用户隔离 MD5 唯一索引");
            assertTrue(reportIndexes.contains("idx_report_task"),
                    "report 应包含 task_id 查询索引");
            assertFalse(reportIndexes.contains("uk_md5"), "不得保留跨用户 uk_md5 约束");
            assertFalse(reportIndexes.contains("task_id"), "不得保留 report.task_id 唯一约束");
            assertTrue(listIndexes(jdbcUrl, "task_step").contains("idx_task_step_idempotency"),
                    "task_step 应包含步骤状态幂等查询索引");
        }
    }

    @Test
    @DisplayName("V6 的 report 唯一约束按用户隔离且允许复用同一 report 的 task_id")
    void shouldEnforceUserScopedDedupeAndAllowReportTaskReuse() throws SQLException {
        try (Connection connection = DriverManager.getConnection(freshJdbcUrl(), USER, PASSWORD)) {
            insertReport(connection, "task-one", 1L, PDF_MD5);
            assertThrows(SQLException.class,
                    () -> insertReport(connection, "task-two", 1L, PDF_MD5),
                    "同用户同 MD5 必须受唯一约束保护");
            insertReport(connection, "task-three", 2L, PDF_MD5);
            insertReport(connection, "task-one", 1L, "fedcba9876543210fedcba9876543210");
        }
    }

    @Test
    @DisplayName("V7 在全新库和既有 V6 库中均把 accounting_check.rule_type 扩容到 VARCHAR(32)")
    void shouldExpandRuleTypeToVarchar32OnFreshAndUpgradedDatabases() throws SQLException {
        for (String jdbcUrl : List.of(freshJdbcUrl(), upgradeJdbcUrl())) {
            // rule_type 列存在
            assertTrue(listColumns(jdbcUrl, "accounting_check").contains("rule_type"),
                    "accounting_check 应包含 rule_type 列");

            // VARCHAR 长度 = 32（CHARACTER_MAXIMUM_LENGTH 反映列定义中的字符长度）
            try (Connection connection = DriverManager.getConnection(jdbcUrl, USER, PASSWORD);
                    Statement statement = connection.createStatement();
                    ResultSet resultSet = statement.executeQuery(
                            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns "
                                    + "WHERE table_schema = DATABASE() "
                                    + "AND table_name = 'accounting_check' "
                                    + "AND column_name = 'rule_type'")) {
                assertTrue(resultSet.next(), "应能查到 rule_type 列元数据");
                assertEquals(32, resultSet.getInt(1),
                        "rule_type 应扩容到 VARCHAR(32)，实际: " + resultSet.getInt(1));
                assertFalse(resultSet.next(), "rule_type 元数据应只有一行");
            }

            // 端到端验证：插入 23 字符的 RuleType.CASH_FLOW_VS_NET_INCOME 不再报 Data too long
            try (Connection connection = DriverManager.getConnection(jdbcUrl, USER, PASSWORD)) {
                long reportId = ensureReportForRuleTypeCheck(connection);
                insertAccountingCheck(connection, reportId,
                        "cash_flow_vs_net_income", "经营现金流 vs 净利润");
            }
        }
    }

    @Test
    @DisplayName("V8 在全新库和既有 V7 库中均把 task/task_step 的 started_at/finished_at 升级到 DATETIME(3)")
    void shouldExpandDatetimePrecisionToMillisOnFreshAndUpgradedDatabases() throws SQLException {
        for (String jdbcUrl : List.of(freshJdbcUrl(), upgradeJdbcUrl())) {
            // task 与 task_step 两张表
            for (String table : List.of("task", "task_step")) {
                for (String column : List.of("started_at", "finished_at")) {
                    try (Connection connection = DriverManager.getConnection(jdbcUrl, USER, PASSWORD);
                            Statement statement = connection.createStatement();
                            ResultSet resultSet = statement.executeQuery(
                                    "SELECT DATETIME_PRECISION FROM information_schema.columns "
                                            + "WHERE table_schema = DATABASE() "
                                            + "AND table_name = '" + table + "' "
                                            + "AND column_name = '" + column + "'")) {
                        assertTrue(resultSet.next(),
                                "应能查到 " + table + "." + column + " 列元数据");
                        assertEquals(3, resultSet.getInt(1),
                                table + "." + column + " 应为 DATETIME(3) 毫秒精度，实际: "
                                        + resultSet.getInt(1));
                        assertFalse(resultSet.next(),
                                table + "." + column + " 元数据应只有一行");
                    }
                }
            }
        }
    }

    private static long ensureReportForRuleTypeCheck(Connection connection) throws SQLException {
        try (Statement statement = connection.createStatement();
                ResultSet resultSet = statement.executeQuery(
                        "SELECT id FROM report WHERE pdf_md5 = '" + PDF_MD5 + "' LIMIT 1")) {
            if (resultSet.next()) {
                return resultSet.getLong(1);
            }
        }
        insertReport(connection, "task-rule-type", 1L, PDF_MD5);
        try (Statement statement = connection.createStatement();
                ResultSet resultSet = statement.executeQuery(
                        "SELECT id FROM report WHERE pdf_md5 = '" + PDF_MD5 + "' LIMIT 1")) {
            assertTrue(resultSet.next(), "插入 report 后必须能查回 id");
            return resultSet.getLong(1);
        }
    }

    private static void insertAccountingCheck(
            Connection connection, long reportId, String ruleType, String ruleName)
            throws SQLException {
        String sql = "INSERT INTO accounting_check "
                + "(report_id, rule_name, rule_type, expected, actual, diff, is_pass, severity) "
                + "VALUES (?, ?, ?, ?, ?, ?, ?, ?)";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setLong(1, reportId);
            statement.setString(2, ruleName);
            statement.setString(3, ruleType);
            statement.setBigDecimal(4, new java.math.BigDecimal("1000.00"));
            statement.setBigDecimal(5, new java.math.BigDecimal("1000.00"));
            statement.setBigDecimal(6, new java.math.BigDecimal("0.00"));
            statement.setByte(7, (byte) 1);
            statement.setString(8, "INFO");
            statement.executeUpdate();
        }
    }

    private static MigrateResult migrate(String jdbcUrl) {
        return Flyway.configure()
                .dataSource(jdbcUrl, USER, PASSWORD)
                .locations("classpath:db/migration")
                .cleanDisabled(true)
                .load()
                .migrate();
    }

    private static MigrateResult migrateToVersion(String jdbcUrl, String targetVersion) {
        return Flyway.configure()
                .dataSource(jdbcUrl, USER, PASSWORD)
                .locations("classpath:db/migration")
                .target(targetVersion)
                .cleanDisabled(true)
                .load()
                .migrate();
    }

    private static void insertReport(Connection connection, String taskId, long userId, String pdfMd5)
            throws SQLException {
        String sql = "INSERT INTO report (task_id, user_id, company_code, company_name, report_type, "
                + "report_period, pdf_md5, pdf_object_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, taskId);
            statement.setLong(2, userId);
            statement.setString(3, "000001");
            statement.setString(4, "测试公司");
            statement.setString(5, "ANNUAL");
            statement.setString(6, "2025");
            statement.setString(7, pdfMd5);
            statement.setString(8, "uploads/" + userId + "/" + pdfMd5 + ".pdf");
            statement.executeUpdate();
        }
    }

    private static int countAppliedMigrations(String jdbcUrl) throws SQLException {
        try (Connection connection = DriverManager.getConnection(jdbcUrl, USER, PASSWORD);
                Statement statement = connection.createStatement();
                ResultSet resultSet = statement.executeQuery(
                        "SELECT COUNT(*) FROM flyway_schema_history WHERE success = 1")) {
            resultSet.next();
            return resultSet.getInt(1);
        }
    }

    private static List<String> listTables(String jdbcUrl, String database) throws SQLException {
        List<String> tables = new ArrayList<>();
        try (Connection connection = DriverManager.getConnection(jdbcUrl, USER, PASSWORD);
                ResultSet resultSet = connection.getMetaData().getTables(
                        database, null, "%", new String[]{"TABLE"})) {
            while (resultSet.next()) {
                tables.add(resultSet.getString("TABLE_NAME"));
            }
        }
        return tables;
    }

    private static List<String> listColumns(String jdbcUrl, String tableName) throws SQLException {
        List<String> columns = new ArrayList<>();
        try (Connection connection = DriverManager.getConnection(jdbcUrl, USER, PASSWORD);
                ResultSet resultSet = connection.getMetaData().getColumns(
                        null, null, tableName, null)) {
            while (resultSet.next()) {
                columns.add(resultSet.getString("COLUMN_NAME"));
            }
        }
        return columns;
    }

    private static List<String> listIndexes(String jdbcUrl, String tableName) throws SQLException {
        List<String> indexes = new ArrayList<>();
        try (Connection connection = DriverManager.getConnection(jdbcUrl, USER, PASSWORD);
                ResultSet resultSet = connection.getMetaData().getIndexInfo(
                        null, null, tableName, false, false)) {
            while (resultSet.next()) {
                String indexName = resultSet.getString("INDEX_NAME");
                if (indexName != null) {
                    indexes.add(indexName);
                }
            }
        }
        return indexes;
    }

    private static String freshJdbcUrl() {
        return jdbcUrl(FRESH_MYSQL, FRESH_DATABASE);
    }

    private static String upgradeJdbcUrl() {
        return jdbcUrl(UPGRADE_MYSQL, UPGRADE_DATABASE);
    }

    private static MySQLContainer<?> mysqlContainer(String databaseName) {
        return new MySQLContainer<>(DockerImageName.parse(MYSQL_IMAGE))
                .withDatabaseName(databaseName)
                .withUsername(USER)
                .withPassword(PASSWORD);
    }

    private static String jdbcUrl(MySQLContainer<?> container, String database) {
        return "jdbc:mysql://" + container.getHost() + ":" + container.getMappedPort(3306) + "/"
                + database + "?useSSL=false&allowPublicKeyRetrieval=true";
    }
}
