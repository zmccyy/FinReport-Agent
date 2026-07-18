# M1.01–M1.12 可靠性修复与重新验收决策

日期：2026-07-17

## 背景

M1 初始实现存在任务越权、上传去重跨用户复用、Progress 消费 ACK 时序、SSE 仅内存恢复、三次重试缺失、readiness 不真实以及 Compose 初始化不可重复验收等问题。

## 决策

1. 所有 taskId 入口均按 JWT 当前用户范围查询；不存在与越权统一返回 RFC 9457 `404 TASK_NOT_FOUND`。
2. 上传以 `userId + pdf_md5` 隔离；Redis SET NX 与数据库约束共同保证 Idempotency-Key 并发语义。失败/取消任务只能使用新 key 创建新任务并保留历史。
3. Progress 在持久化成功后才 ACK；失败 NACK 至 DLQ。重试通过 `x-retry-count` 和 1/5/30 秒 TTL 队列执行，最多三次。
4. Redis 成为 SSE 恢复来源：24 小时 TTL、256 条事件、`taskId:sequence` 单调事件 ID；本地 emitter pool 仅实时 fan-out。
5. `/internal/live` 与 `/internal/health` 分离；后者探测 MySQL、Redis、RabbitMQ、MinIO 上传 bucket 和 AI 健康端点。
6. 开发环境只能通过 base Compose 与 dev overlay 合并启动；`minio-init` 是一次性服务，MinIO shell 脚本固定 LF 以兼容 Linux 容器。
7. 前端 dev overlay 以 `!override` 覆盖生产 80 端口，默认改映射到宿主机 5173，避免 Windows HTTP.sys 端口保留冲突。

## 已完成的基础设施验收

- 后端 `/internal/live` 和 `/internal/health` 返回 200；readiness 中五个依赖均为 `UP`。
- RabbitMQ 已载入 7 个应用 exchange、15 个 durable queue、28 个 binding，含 DLQ 与 1/5/30 秒 TTL retry queue。
- MinIO 初始化已创建六个业务 bucket 与 `a-bucket`，确认 private/report-download 策略及 90/7 天生命周期规则。
- Milvus 已实际创建并复跑验证 `fin_kb` collection、HNSW(IP, M=16, efConstruction=200) 索引与 loaded 状态。

## 最终重新验收（2026-07-17）

### 追加修复

端到端演练首次发现 Docker 环境只报告 MySQL 可连通、但 `user_account` 表不存在；注册请求稳定返回 `500`。根因是应用只启用了 R2DBC，Spring Boot 的 JDBC Flyway 自动配置不会为其创建 JDBC `DataSource`，因此未执行迁移。新增显式 `FlywayMigrationConfiguration`，以 `spring.datasource.*` 在后端启动期执行既有 V1–V6 migration。保留历史 migration 不变。

### 命令与实机结果

- `backend/.\mvnw.cmd test`：186 tests passed。
- `backend/.\mvnw.cmd verify`：passed，JaCoCo 核心覆盖率门禁通过。
- `backend/.\mvnw.cmd checkstyle:check spotbugs:check`：0 Checkstyle violations，0 SpotBugs warnings。
- `backend/.\mvnw.cmd verify -Pintegration`：passed；Testcontainers 验证干净库 V1→V6 及已存在 V5→V6 升级。
- `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build`：重复执行通过；9 个常驻服务 healthy，`minio-init` exited 0。运行中后端日志确认 V1–V6 migration 成功，MySQL 实机存在全部 M1 表。
- 真实 HTTP/MQ/SSE 演练：注册/登录、上传、同 key 原响应复用、同 key 不同文件 `409 IDEMPOTENCY_KEY_REUSED`、不同用户同 MD5 隔离、跨用户详情/取消 `404`、RabbitMQ 注入 PARSE/EXTRACT_BS/EXTRACT_IS/EXTRACT_CF/CHECK/REPORT 后任务 `COMPLETED`、`Last-Event-ID` 回放 sequence 1、取消任务以新 key 创建新 task 并复用 report，均通过。
- 两个并行的真实上传请求使用同一用户及同一 Idempotency-Key，均返回 `201` 且得到同一个 taskId/reportId。

## 范围与后续

- M1.01–M1.12 已重新验收完成。
- M1.13 及之后的 AI、前端业务和 CI 任务仍未完成；它们不因本次基础链路验收而被标记为完成。M1.17 的 CI 三条流水线仍是独立待办。
