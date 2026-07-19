# M1 L3 Mock 链路、MQ 拓扑与 E2E 验收决策

**日期**：2026-07-19
**背景**：M1 需要在真实 Compose 环境中完成“上传 PDF → L3 mock 处理 → RabbitMQ progress → L2 SSE → 前端完成态”的骨架闭环，并为后续任务提供 CI 门禁。

## 决策

1. L3 使用 FastAPI mock service，提供 `/internal/health` 和 `/parse`，MQ worker 作为应用生命周期中的后台线程运行；CI dry-run 通过 `AI_SERVICE_MQ_CONSUMER_ENABLED=false` 关闭 worker。
2. L3 worker 对 `q.parse.requests`、`q.extract.requests`、`q.reason.requests` 使用 `prefetch_count=1` 和手动 ack；失败 `nack(requeue=false)`，由 RabbitMQ DLQ 保留失败消息。
3. 任务重试使用 `task.retry.1s.exchange`、`task.retry.5s.exchange`、`task.retry.30s.exchange` 与 TTL retry queue；运行时声明与 `deploy/rabbitmq/definitions.json` 由测试校验一致。
4. PARSE 成功后必须完整投递 `extract.bs`、`extract.is`、`extract.cf`，避免三表勾稽前的状态机永久 pending。
5. CI 拆分为 Backend、AI Service、Frontend 三条 GitHub Actions 工作流；Frontend 工作流真实启动 Compose 并执行 Playwright E2E。

## 修复的缺陷

- Windows CRLF 使 Linux 容器无法执行 Maven wrapper / MinIO 初始化脚本：以 `.gitattributes` 固定 shell wrapper 的 LF。
- 前端 dev overlay 少了 `/api/v1` 前缀，导致 API 请求路径错误。
- 长空闲 RabbitMQ 关闭 Pika 连接后，consumer 曾退出且 producer 不能恢复：增加可中断重连循环与一次可靠重发。
- AI CI 缺失 coverage failure gate：增加 `--cov-fail-under=80`。
- E2E 曾因页面上重复的“解析完成”文本触发 Playwright strict-mode 错误：改为定位完成面板 `.done__title`。
- E2E 的截图目录为 Windows 绝对路径，不能可靠作为 Linux CI artifact：改为仓库内可移植的 `artifacts/m116`，并忽略运行产物。
- `JwtFilter` 在 CORS filter 之前拦截受保护接口的 `OPTIONS`：显式放行预检，使 WebFlux CORS 配置返回 allow headers；真实 `GET` / `POST` 仍保持 JWT 校验。

## 已完成验证

- Backend：`mvnw.cmd test`、`verify`（JaCoCo 核心门槛）、`checkstyle:check spotbugs:check`、`verify -Pintegration` 全部通过。
- AI：ruff、black、49 个 pytest、compileall、RabbitMQ 拓扑 dry-run 通过；本机没有 Python 3.11 / pytest-cov，80% 失败门槛由新增的 Python 3.11 GitHub Actions 执行。
- Frontend：lint、type-check、production build 通过。构建仍报告 VueUse PURE 注释和主 bundle 大于 500KB 的非阻断警告。
- Compose：backend、ai-service、frontend、MySQL、Redis、RabbitMQ、MinIO、Milvus、etcd 均 healthy，`minio-init` 为 Exited (0)。
- 浏览器：`scripts/e2e_m116.py` 完整通过；受保护 `OPTIONS /api/v1/users/me` 返回 200 并带完整 CORS allow headers。
- 数据验收：任务 `task-b7d80ff3bb06` 完成；`task.id` 为任务主键，相关 6 条 `task_step`（按 `task_step.task_id` 关联）均为 SUCCESS；MinIO 可列出上传的贵州茅台 PDF。

## 风险与后续行动

- GitHub CLI 在本机不可用；必须使用 Git 凭据推送分支后，在 GitHub Actions 页面确认三条远程流水线全部成功，随后才勾选 M1.17 与阶段验收中的 CI 项。
- 6GB VRAM 约束、真实模型接入和 QLoRA 训练仍属于后续里程碑；M1 仅交付 mock 处理链。
- 前端主 bundle 约 1.24MB，建议在 M6 通过路由/依赖拆分处理。
