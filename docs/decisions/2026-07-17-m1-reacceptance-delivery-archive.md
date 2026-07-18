# M1.01–M1.12 修复与重新验收交付归档

日期：2026-07-17
分支：`feature/M1.12-reliability-hardening`
关联决策：

- 审查基线：[`2026-07-16-m1-01-to-m1-12-deep-review.md`](2026-07-16-m1-01-to-m1-12-deep-review.md)
- 修复决策与验收明细：[`2026-07-17-m1-reliability-remediation.md`](2026-07-17-m1-reliability-remediation.md)

## 归档目的

本记录将 M1.01–M1.12 的深度审查、修复实施、重新验收和交付范围集中归档，作为从“不可验收”到“重新验收通过”的可追溯证据。2026-07-16 的审查结论曾明确禁止将 M1.01–M1.12 标记为完成；本轮依据同一审查发现逐项修复，并在真实 Docker 和端到端链路中复验。

## 审查发现到修复的对应关系

| 审查风险 | 修复措施 | 验收证据 |
|---|---|---|
| dev overlay 不能独立构成环境，backend dev 镜像缺 Maven Wrapper | 统一为 base + dev overlay Compose 命令；Dockerfile 复制 `mvnw` 与 `.mvn`；文档同步 | 干净环境多次执行合并 Compose，常驻服务 healthy |
| MinIO、RabbitMQ、Milvus 初始资源不可重复验证 | 增加幂等 `minio-init`，固化 bucket/策略；声明 durable exchange/queue/DLQ/TTL retry；检查 Milvus collection/index | `minio-init` exited 0；RabbitMQ topology 与 Milvus HNSW index 实查 |
| 任务详情、SSE、取消可能跨用户访问 | repository / controller 全部以当前 JWT `userId` scoped 查询 | 跨用户详情和取消均返回 RFC 9457 `404 TASK_NOT_FOUND` |
| Progress 消费异常仍 ACK 导致消息丢失 | 仅在任务状态持久化成功后 ACK；解析、状态机、数据库、编排异常 NACK 至 DLQ；广播失败仅告警 | MQ 单测、真实队列注入和最终任务状态完成验证 |
| 缺失 3 次可追踪重试 | `x-retry-count` + 1/5/30 秒持久化 TTL retry queue；第 3 次后 FAILED | 单测覆盖 header、路由、退避和最终失败边界 |
| SSE 只依赖进程内回放 | Redis 持久化 snapshot 和最近 256 事件（24h TTL），原子 sequence，`taskId:sequence` 事件 ID | `Last-Event-ID` 回放、pool 重建后 Redis 恢复单测及实机 SSE 演练 |
| `pdf_md5` 全局唯一造成跨用户报告复用，上传竞态不受控 | Flyway V6 改为 `(user_id, pdf_md5)` 唯一；Redis SET NX claim + DB 约束兜底；失败/取消任务使用新 key 重试 | 同 key 同响应、同 key 异文件 409、同 PDF 异用户隔离、并发请求同 task/report |
| readiness 只反映进程而非依赖可用性 | 拆分 `/internal/live` 与真实 `/internal/health`，对 MySQL、Redis、RabbitMQ、MinIO bucket 和 AI 健康端点设置有界探测 | Docker 环境 health 均返回 200，依赖状态为 UP |
| R2DBC 应用未触发 JDBC Flyway 自动配置 | 新增显式 `FlywayMigrationConfiguration` 使用 `spring.datasource.*` 执行 V1–V6 | Docker 初始库注册成功；`flyway_schema_history` 与 M1 表实查 |
| 覆盖率 DoD 不可证明 | POM 配置 `jacoco:check` 并在 `verify` 生效；核心业务包 >= 80% | `verify` 生成 XML/HTML 且门禁通过 |

## 实施范围

本轮代码、配置、测试和文档变更已经在提交 `3077341` 中交付，覆盖：

- 后端：任务授权、任务编排与状态机、上传幂等、RabbitMQ consumer/producer、Redis SSE、健康检查、Flyway、JaCoCo；
- 部署：Compose、MinIO 初始化、RabbitMQ definitions；
- 测试：Controller、Security、MQ、SSE、File、Orchestrator、Flyway 迁移与 Testcontainers 集成测试；
- 文档：README、OpenAPI、部署说明、M1 进度和修复决策。

本次补充提交同时纳入此前工作区中保留的审查原件、下载辅助脚本和三份样例年报，以便远程仓库保留审查上下文和可复现的上传样本。

## 重新验收记录

以下命令于 2026-07-17 在 `backend` 目录完成并通过：

```powershell
.\mvnw.cmd test
.\mvnw.cmd verify
.\mvnw.cmd checkstyle:check spotbugs:check
.\mvnw.cmd verify -Pintegration
```

结果：

- `test`：186 个测试通过；
- `verify`：JaCoCo 核心覆盖率门禁实际执行并通过，产物为 `target/site/jacoco/jacoco.xml` 和 HTML 报告；
- Checkstyle：0 violations；SpotBugs：0 warnings；
- 集成验证：Testcontainers 验证全新数据库 V1→V6 与既有 V5→V6 升级。

真实环境命令：

```powershell
cd deploy
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

验证结果：9 个常驻服务 healthy（`ai-service`、`backend`、`etcd`、`frontend`、`milvus`、`minio`、`mysql`、`rabbitmq`、`redis`），`minio-init` 正常退出（0）。端到端演练覆盖注册/登录、PDF 上传、对象和任务落库、六阶段 progress、SSE 断线补发、跨用户拒绝、同 key 并发、同/异用户 MD5 和失败后新 key 重试。

## 本次补充归档清单

| 路径 | 内容 | 目的 |
|---|---|---|
| `docs/decisions/2026-07-16-m1-01-to-m1-12-deep-review.md` | 原始深度审查记录 | 保留发现、证据和修复基线 |
| `data/sample_reports/` | 3 份 2025 年年度报告 PDF 样本 | 支持本地上传与链路复现 |
| `scripts/_test_cninfo.py` | CNINFO 连通性辅助脚本 | 采样来源诊断 |
| `scripts/_test_orgid.py` | CNINFO 组织标识查询辅助脚本 | 采样来源诊断 |
| `scripts/dl2.py` | 下载辅助脚本 | 样本获取过程留档 |
| `scripts/download_sample.py` | 年报样本下载脚本 | 样本获取过程留档 |

## 范围边界

- **已重新验收**：M1.01–M1.12。
- **未因本次验收完成**：M1.13 及之后的 AI、前端业务与 CI 工作；M1.17 CI 流水线仍是独立待办。
- 本记录不将后续里程碑误标为完成，仅保存 M1 基础链路的实施与验收事实。
