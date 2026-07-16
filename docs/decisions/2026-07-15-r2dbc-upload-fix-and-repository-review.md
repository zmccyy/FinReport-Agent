# R2DBC 上传持久化修复与仓库审查决策记录

> **日期**：2026-07-15（更新：2026-07-16）
> **范围**：M1.12 `POST /api/v1/reports/upload` 与 M1.08–M1.12 后端可靠性审查
> **状态**：核心修复完成；单元验证、构建和静态检查通过；真实 MySQL 集成测试待 Docker 环境恢复后执行

---

## 背景

使用 `curl` 验证 M1.12 上传接口时，创建初始任务会传入
`Task.refReportId=null` 和 `Task.currentStep=null`。原实现把这两个值直接传给
R2DBC `DatabaseClient.GenericExecuteSpec.bind()`。该 API 不接受 `null`，所以在 SQL
执行前失败，导致带手动生成 task ID 的上传任务无法持久化。

本次同时审查了上传、任务编排、SSE、消息投递、链路追踪、JWT 配置和 Maven 测试链路，
并修复了与该上传链路直接相关的高优先级问题。

## 决策与已完成修复

### D1：R2DBC 可空字段使用显式类型绑定

`TaskOrchestrator.insertTask()` 对 `refReportId` 和 `currentStep` 使用
`bindNull(name, type)`，非空值仍使用 `bind(name, value)`。保留现有应用侧 task ID
生成方案，同时让 SQL `NULL` 正确传递给 R2DBC 驱动。

### D2：task 与 task_step 在同一响应式事务中初始化

通过 `TransactionalOperator` 包住 task 与初始 task_step 写入，避免步骤写入中途失败时
残留不完整任务定义。任务后续业务失败仍记录为 `FAILED`，不会回滚诊断数据。

### D3：RabbitMQ 发布失败显式补偿为 FAILED

调度步骤在更新为 `RUNNING` 后，如发布 RabbitMQ 消息发生 `IntegrationException`，会将
任务和当前 task_step 置为 `FAILED`、记录错误信息和完成时间，并重新抛出原异常。该修复
避免任务静默停留在 `RUNNING`。

### D4：SSE 串行发射并保存订阅前事件

每个 task 的 replay sink 使用独立 monitor 串行化 `emit` 与 `complete`，不再用有限次数的
`FAIL_NON_SERIALIZED` 重试丢弃事件。进一步发现并修复了“先收到 MQ 进度、后建立 SSE
订阅”时事件被直接丢弃的问题：`emit()` 现在会创建对应 replay sink，晚订阅者仍能收到
已缓存的进度事件。

### D5：上传改为临时文件流式处理

PDF 数据块流式写入临时文件并按块检查 50 MB 限制；MD5 从文件流计算；MinIO 上传与文件
I/O 切换到 `boundedElastic`；`usingWhen` 负责成功和失败后的临时文件清理；同时对取消或
错误路径中未消费的 `DataBuffer` 执行释放。

### D6：traceId 从 HTTP 透传至 MQ

新增 WebFlux `TraceIdWebFilter`：优先接受 `X-Trace-Id`，缺失时生成 UUID，并回写响应头、
exchange attribute 与 Reactor Context。任务编排从 Reactor Context 取值，MQ 发布时写入
`traceId` header；异常响应从 exchange attribute 读取 traceId，避免依赖跨线程 MDC。

### D7：禁止生产环境回退到可预测 JWT 密钥

根配置取消 JWT 默认值；仅 `local` profile 配置开发密钥。启动校验要求密钥非空、至少
32 字符，且非 local profile 不得使用本地开发密钥。

### D8：测试分层和 Maven 工具链

新增 Maven Wrapper、Checkstyle、SpotBugs 和 JaCoCo 配置。真实 MySQL 的 Flyway 测试改名为
`FlywayMigrationIT`，仅在 `-Pintegration` 启用；默认 `verify` 明确跳过集成测试，避免没有
数据库时把单元测试流水线误判为失败。补充 Surefire 的 JaCoCo `argLine` 传递配置，供 ASCII
CI 工作目录生成覆盖率报告。

## 验证记录

### HTTP 上传回归

此前在本机 `local` profile 启动后端后，使用真实 curl 依次完成注册、获取 JWT 和上传：

- 文件：`data/sample_reports/000001_平安银行_2025年年度报告.pdf`
- 请求 traceId：`m112-curl-8b97c7d5-dfed-44c8-a9c4-4104a10964ee`
- 上传结果：HTTP `201 Created`
- 响应：`{"taskId":"task-09276ea4ee82","reportId":1,"status":"PENDING"}`
- 响应 `X-Trace-Id` 与请求值一致。

该结果覆盖了手动 task ID、两个可空字段的 R2DBC 写入、报告创建和 traceId 回传。

### 自动化与静态检查（2026-07-16）

- `backend\mvnw.cmd -Dtest=SseEmitterPoolTest test`：10 tests，0 failures，0 errors。
- `backend\mvnw.cmd test`：131 tests，0 failures，0 errors。
- `backend\mvnw.cmd verify`：构建成功；131 tests 通过；默认跳过真实 MySQL 集成测试。
- `backend\mvnw.cmd checkstyle:check`：0 violations。
- `backend\mvnw.cmd spotbugs:check`：0 findings。

## 剩余风险与建议

1. **真实 MySQL 集成测试未在本轮执行。** 2026-07-16 本机 Docker Desktop daemon 不可连接，
   `-Pintegration` 下的 `FlywayMigrationIT` 因 `localhost:3306` 拒绝连接而无法执行。恢复 Docker
   后应使用 `docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up -d mysql`
   再运行 `backend\mvnw.cmd verify -Pintegration`。
2. **未取得本工作目录的 JaCoCo 覆盖率数值。** 当前 Windows 工作目录
   `E:\项目\FinReport Agent` 含 Unicode 与空格，Surefire 产生 boot manifest classpath root 警告，
   JaCoCo execution data 未落盘。因此不能声称已达到 80% 覆盖率门槛。应在 CI 的 ASCII 工作目录
   验证 `target/site/jacoco/index.html` 并配置门槛；在结果达到要求前，此项不应作为 M1 完整 DoD。
3. **数据库与 RabbitMQ 不是分布式原子事务。** 当前补偿能避免静默 RUNNING 状态，但仍存在数据库
   已提交、进程在 MQ 发布前崩溃的窗口。M2 前建议引入 transactional outbox 与可重放 relay。
4. **SSE replay 历史目前固定为 16 条。** 对长任务或断线较久的客户端可能不足；应结合 Redis
   `fin:task:progress:{taskId}` 实现 Last-Event-ID 恢复。
5. **开发 compose 文件是覆盖文件。** 单独执行 `docker compose -f deploy/docker-compose.dev.yml up -d`
   缺少基础 service 定义；开发文档与脚本应统一使用 base + dev 两个 compose 文件。

## 完成 Checklist

- [x] 修复 R2DBC 手动 ID 实体的可空字段绑定
- [x] 增加 R2DBC nullable bind、事务和 MQ 失败补偿测试
- [x] 实际 curl 上传返回 HTTP 201 并回传相同 traceId
- [x] 修复 SSE 并发 emit 和 emit-before-subscribe 回放缺陷
- [x] 上传改为流式临时文件处理并隔离阻塞 I/O
- [x] 修复 traceId 透传和 JWT 安全默认值
- [x] 通过单元测试、默认构建、Checkstyle 与 SpotBugs
- [ ] Docker/MySQL 恢复后执行 `verify -Pintegration`
- [ ] 在 ASCII CI 工作目录确认 JaCoCo 覆盖率并落实 >=80% 门槛
