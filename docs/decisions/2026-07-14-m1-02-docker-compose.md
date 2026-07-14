# 2026-07-14 M1.02 Docker Compose 编排 8 Service

> 摘要：创建 docker-compose.yml / docker-compose.dev.yml 及 3 个 Dockerfile，最小可运行应用骨架

## 关键决策

| # | 决策 | 背景 |
|---|---|---|
| D1 | **Milvus 共用 MinIO** | 使用项目 MinIO 作为 Milvus 对象存储，避免额外实例 |
| D2 | **ai-service 不依赖 MySQL** | AI 服务通过 MQ 与后端解耦，直接依赖 Redis + RabbitMQ + MinIO |
| D3 | **生产用命名卷、开发用绑定挂载** | 生产 compose 使用 Docker 管理的命名卷；dev compose 覆盖为源码绑定挂载 |
| D4 | **Dockerfile 三阶段构建** | build / runtime / dev 三个阶段，dev 阶段支持热重载 |
| D5 | **M1 期间 RabbitMQ 不加载 definitions** | 拓扑声明由 M1.06 scripts/declare_mq.py 完成，避免定义文件与 env var 冲突 |
| D6 | **前端用 Nginx 反代 /api/** | Vue history mode + `/api/` proxy → backend:8080，SSE 关闭 proxy_buffering |

## 创建文件清单

### 核心编排
- `deploy/docker-compose.yml` — 9 容器（含 etcd），bridge 网络，7 个持久化卷
- `deploy/docker-compose.dev.yml` — 源码挂载 + 热重载覆盖
- `deploy/.env.example` — 15 个可配置环境变量

### 配置
- `deploy/mysql/init.sql` — 字符集 + 权限初始化
- `deploy/rabbitmq/rabbitmq.conf` — 内存/磁盘限制 + 管理插件
- `deploy/rabbitmq/definitions.json` — 占位定义（M1.06 扩展）

### Backend (SpringBoot 3.2.x / Java 21)
- `backend/Dockerfile` — 3 阶段（maven:3.9 → eclipse-temurin:21-jre → dev）
- `backend/pom.xml` — WebFlux + Security + R2DBC + Redis + RabbitMQ + MinIO + JWT
- `backend/src/main/java/com/finreport/FinReportApplication.java`
- `backend/src/main/java/com/finreport/config/SecurityConfig.java` — 暂时 permitAll()
- `backend/src/main/java/com/finreport/controller/HealthController.java` — `/api/v1/system/health`
- `backend/src/main/resources/application.yml` — local + docker profiles
- `backend/.dockerignore`

### AI Service (FastAPI / Python 3.11)
- `ai-service/Dockerfile` — 2 阶段（python:3.11-slim → dev）
- `ai-service/pyproject.toml` — FastAPI + pika + redis + pymilvus + minio
- `ai-service/app/main.py` — 应用入口 + lifespan
- `ai-service/app/api/health.py` — `/internal/health` 端点（已验证 200 OK）
- `ai-service/.dockerignore`

### Frontend (Vue3 / Vite / Nginx)
- `frontend/Dockerfile` — 3 阶段（node:20 → nginx:1.27-alpine → dev）
- `frontend/package.json` — Vue3 + Pinia + Element Plus + axios
- `frontend/vite.config.ts` — 路径别名 @/ + proxy
- `frontend/tsconfig.json` + `tsconfig.node.json`
- `frontend/env.d.ts` — Vue SFC + ImportMeta 类型声明
- `frontend/index.html`
- `frontend/nginx.conf` — Gzip + API proxy + history mode + 静态缓存
- `frontend/src/main.ts` — 应用入口
- `frontend/src/App.vue` — 根组件
- `frontend/src/router/index.ts` — Vue Router
- `frontend/src/views/Home.vue` — 占位首页
- `frontend/src/api/http.ts` — axios 封装（traceId + JWT 拦截器）
- `frontend/src/api/sse.ts` — SSE 客户端（自动重连 + 事件路由）
- `frontend/.dockerignore`

## 验证结果

- [x] `docker compose config` 验证通过
- [x] FastAPI `/internal/health` 端点返回 200 + `{"status":"UP"}`
- [x] 所有 9 个 service 均配置 healthcheck
- [x] 依赖链正确：milvus→etcd+minio, backend→mysql+redis+rabbitmq, ai-service→redis+rabbitmq+minio, frontend→backend
- [x] Dev compose 覆盖正确：app 服务挂载源码 + 热重载

## 已知限制

- 应用 Dockerfile 未做实际镜像构建测试（需 Docker 拉取基础镜像）
- Maven 构建依赖首次下载较慢（~5min），后续利用缓存
- Milvus standalone 启动需 40-60s（start_period 已设置 40s）

## 下一步

- [ ] M1.03 MySQL 建表 + Flyway 迁移
- [ ] 可选：运行 `docker compose build` 测试 Dockerfile
