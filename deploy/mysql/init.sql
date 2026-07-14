-- FinReport Agent — MySQL 初始化脚本
-- 在容器首次启动时由 /docker-entrypoint-initdb.d/ 自动执行
-- 版本：v1.0 | 日期：2026-07-14

-- 确保使用 utf8mb4
ALTER DATABASE finreport CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 授权 finreport 用户（如果通过 MYSQL_USER 创建）
GRANT ALL PRIVILEGES ON finreport.* TO 'finreport'@'%';
FLUSH PRIVILEGES;
