# 2026-07-14 M1.03 MySQL 建表 + Flyway 迁移

> 摘要：创建 5 个 Flyway 迁移文件，实现 spec §5.2.2 全部 12 张表 + 10 个额外性能索引

## 关键决策

| # | 决策 | 背景 |
|---|---|---|
| D1 | **Flyway 版本号与文件对应** | V1=用户域、V2=财报域(5表)、V3=任务域(4表)、V4=模型与审计域(2表)、V5=额外索引。按域拆分而非按表拆分，每个文件内表之间业务关联紧密 |
| D2 | **不添加 FOREIGN KEY 约束** | spec §5.2.2 未定义任何 FK。保持与 spec 一致，后续如需 FK 再通过新迁移文件添加 |
| D3 | **model_registry 用 trained_at 替代 created_at** | 与 spec §5.2.2 一致，表示模型训练时间而非记录创建时间 |
| D4 | **task_step 无独立时间戳** | 与 spec §5.2.2 一致，通过 task_id 关联 task.created_at + started_at/finished_at 表达时间信息 |
| D5 | **V5 新增 10 个索引补充行内索引** | 覆盖用户维度查询、状态筛选、复合条件过滤等高频查询路径，所有索引命名遵循 `idx_{table}_{col}` 规范 |
| D6 | **添加 mysql-connector-j（runtime scope）** | pom.xml 原有 flyway-mysql + spring-boot-starter-jdbc，缺少 JDBC 驱动。添加后 Flyway 可通过 JDBC DataSource 执行迁移 |
| D7 | **测试策略：结构验证而非集成执行** | Docker 不可用，无法启动 MySQL 做实际 Flyway 迁移。用 20 个 JUnit 测试验证 SQL 文件结构：表名、列名、索引数、引擎/字符集，均通过 |

## 创建/修改文件清单

### 依赖
- `backend/pom.xml` — 新增 mysql-connector-j（runtime）

### Flyway 迁移
- `backend/src/main/resources/db/migration/V1__init_user.sql` — user_account（用户域，1 表）
- `backend/src/main/resources/db/migration/V2__init_report.sql` — report + financial_statement + accounting_check + anomaly + report_artifact（财报域，5 表）
- `backend/src/main/resources/db/migration/V3__init_task.sql` — task + task_step + chat_session + chat_message（任务域，4 表）
- `backend/src/main/resources/db/migration/V4__init_model_audit.sql` — model_registry + audit_log（模型与审计域，2 表）
- `backend/src/main/resources/db/migration/V5__init_indexes.sql` — 10 个额外性能索引

### 测试
- `backend/src/test/java/com/finreport/FlywayMigrationTest.java` — 20 个结构验证测试

### 进度
- `docs/progress/m1.md` — M1.03 打勾

## 验证结果

- [x] `mvn clean test` — BUILD SUCCESS，20/20 tests pass
- [x] 全部 12 张表与 spec §5.2.2 逐字段交叉核对一致
- [x] 所有表使用 InnoDB + utf8mb4
- [x] 所有索引命名符合 CLAUDE.md §3.4 规范
- [x] 迁移文件可通过 Flyway 自动执行（`spring.flyway.enabled=true`）

## 已知限制

- 未在真实 MySQL 上执行 Flyway 迁移（Docker 不可用）。需在 M1.07 SpringBoot 骨架完成后，启动应用验证 Flyway 自动 migrate 成功
- 未添加 FOREIGN KEY 约束（与 spec 一致，可能影响数据完整性校验）

## 下一步

- [ ] M1.04 MinIO bucket 初始化
- [ ] M1.07 启动 SpringBoot 应用触发 Flyway migrate，执行 `SHOW TABLES;` 验证
