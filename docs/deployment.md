# 开发部署与验收说明

## Compose 使用方式

从仓库根目录进入 `deploy` 后，只能以 base 文件叠加开发 overlay 的方式启动完整开发栈：

```powershell
cd deploy
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

`docker-compose.dev.yml` 只覆盖开发阶段（源码挂载、热重载、调试端口和 Vite），不能独立启动。Windows 开发环境默认将 Vite 映射到 `5173`，避免占用常被 HTTP.sys 保留的宿主机端口 80；可通过 `FRONTEND_PORT` 覆盖。

停止：

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml down
```

## 服务验收

| 类别 | 服务 | 期望状态 |
|---|---|---|
| 常驻应用 | frontend、backend、ai-service、mysql、redis、rabbitmq、minio、milvus | `healthy` |
| 依赖 | etcd | `healthy` |
| 一次性初始化 | minio-init | `Exited (0)` / `completed successfully` |

`minio-init` 幂等创建 spec §5.5.1 的六个业务 bucket，并为 Milvus 创建 `a-bucket`；开发策略只允许 `finreport-reports` 匿名下载，其他 bucket 私有。它还配置 `finreport-uploads` 90 天和 `finreport-artifacts` 7 天的生命周期规则。

## 基础设施检查

```powershell
# MinIO bucket 和策略
docker exec finreport-minio mc ls local
docker exec finreport-minio mc anonymous get local/finreport-reports

# RabbitMQ durable queue、DLQ、TTL retry queue
docker exec finreport-rabbitmq rabbitmqctl list_exchanges name type durable
docker exec finreport-rabbitmq rabbitmqctl list_queues name durable arguments

# Milvus schema/index（宿主机需已安装 pymilvus）
python ..\scripts\init_milvus.py --host localhost --port 19530
```

RabbitMQ 拓扑必须包含 durable 主队列、DLQ 和 `q.task.retry.1s`、`q.task.retry.5s`、`q.task.retry.30s` 三个 TTL 重试队列。消息保持 delivery mode 2、`prefetch_count=1`，重试次数通过 `x-retry-count` header 传递。

## 后端探针

```powershell
curl.exe -i http://localhost:8080/internal/live
curl.exe -i http://localhost:8080/internal/health
```

`/internal/live` 仅检查进程；`/internal/health` 需要 MySQL、Redis、RabbitMQ、MinIO 上传 bucket 和 AI 服务均可用。探针无须 Bearer token，供容器编排使用。
