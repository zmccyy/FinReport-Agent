# FinReport Agent

A 股上市公司财报深度解析 Agent。M1.01–M1.12 覆盖开发基础设施、认证、PDF 上传、任务编排、进度消息与 SSE 恢复基础能力。

## 前置条件

- Docker Desktop（含 Compose v2）
- Java 21（用于后端测试与质量门禁）
- Python 3.11（AI 服务开发；Milvus 初始化脚本需要 `pymilvus`）
- 开发机显存约束：RTX 4050 Mobile 仅 6 GB VRAM。模型推理必须遵循 **4-bit 量化 + QLoRA**；不得并发装载多个 7B 模型。

## 启动与停止开发栈

`docker-compose.dev.yml` 是开发覆盖层，**不能单独运行**。从 `deploy` 目录使用唯一的完整启动命令：

```powershell
cd deploy
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

检查和停止：

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml ps
docker compose -f docker-compose.yml -f docker-compose.dev.yml down
```

验收时应区分两类服务：

- 8 个常驻应用服务：`frontend`、`backend`、`ai-service`、`mysql`、`redis`、`rabbitmq`、`minio`、`milvus`，应为 `healthy`；`etcd` 作为 Milvus 依赖也应为 `healthy`。
- `minio-init` 是一次性初始化服务，正确状态为 `Exited (0)` / `completed successfully`，不是 `healthy`。

开发端口：前端 `http://localhost:5173`、后端 `http://localhost:8080`、AI 服务 `http://localhost:8000`、RabbitMQ 管理台 `http://localhost:15672`、MinIO Console `http://localhost:9001`、Milvus `localhost:19530`。

启动后执行一次 Milvus collection 初始化（可重复运行）：

```powershell
cd ..
python scripts/init_milvus.py --host localhost --port 19530
```

## 健康检查

- `GET /internal/live`：只反映后端进程是否运行；用于 liveness，不因下游短暂不可用触发重启。
- `GET /internal/health`：真实 readiness。以有界超时检查 MySQL、Redis、RabbitMQ、`finreport-uploads` MinIO bucket 和 AI 服务 `/internal/health`；任一组件不可用返回 `503 DOWN`。

## 后端验证

```powershell
cd backend
.\mvnw.cmd test
.\mvnw.cmd verify
.\mvnw.cmd checkstyle:check spotbugs:check
.\mvnw.cmd verify -Pintegration
```

`verify` 会执行 JaCoCo 门禁：M1 核心 controller、orchestrator、file、SSE、MQ、security 包行覆盖率不低于 80%。报告位于 `backend/target/site/jacoco/`（HTML）与 `backend/target/site/jacoco/jacoco.xml`（XML）。

## M1 重新验收流程

1. 用上述合并 Compose 命令启动，确认常驻服务和 `minio-init` 状态。
2. 执行 `scripts/init_milvus.py`，验证 `fin_kb` collection、HNSW 索引和 loaded 状态。
3. 注册、登录，携带 `Idempotency-Key` 上传 PDF，确认 MinIO 对象、report、task 和 task_step 落库。
4. 注入 PARSE、EXTRACT、CHECK、REPORT progress；断开 SSE 后带 `Last-Event-ID: {taskId}:{sequence}` 重连，验证 Redis replay 或最新快照回退。
5. 验证跨用户获取、订阅和取消 task 均为 `404 TASK_NOT_FOUND`；验证同用户/跨用户 MD5 以及同 key 并发语义。
6. 使用上面的 Maven 命令完成单元、覆盖率、静态检查和 Testcontainers 验收。

接口语义详见 `docs/api/openapi.yaml`，部署细节详见 `docs/deployment.md`。
