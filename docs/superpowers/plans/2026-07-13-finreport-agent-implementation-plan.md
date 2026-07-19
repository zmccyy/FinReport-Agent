# FinReport Agent 实现计划

> **配套设计文档**：[2026-07-13-finreport-agent-design.md](../specs/2026-07-13-finreport-agent-design.md)
> **版本**：v1.1
> **日期**：2026-07-19（最后更新）
> **状态**：M1 已完成，M2 待开始
> **总周期**：12 周 / 6 个里程碑 / 约 720h

---

## 目录

- [0. 计划总览](#0-计划总览)
- [1. 仓库与目录结构](#1-仓库与目录结构)
- [2. 通用约定](#2-通用约定)
- [3. M1 基础设施与骨架打通（Week 1-2）](#3-m1-基础设施与骨架打通week-1-2)
- [4. M2 解析与抽取闭环（Week 3-4）](#4-m2-解析与抽取闭环week-3-4)
- [5. M3 勾稽异常与报告生成（Week 5-6）](#5-m3-勾稽异常与报告生成week-5-6)
- [6. M4 模型微调 T1/T2/T3（Week 7-8）](#6-m4-模型微调-t1t2t3week-7-8)
- [7. M5 Agent 问答与知识库（Week 9-10）](#7-m5-agent-问答与知识库week-9-10)
- [8. M6 前端打磨评估与发布（Week 11-12）](#8-m6-前端打磨评估与发布week-11-12)
- [9. 跨阶段并行与依赖](#9-跨阶段并行与依赖)
- [10. 风险与缓冲](#10-风险与缓冲)
- [11. Definition of Done](#11-definition-of-done)

---

## 0. 计划总览

### 0.1 目标

按设计文档 §8 的 6 个里程碑，将"上传 PDF → 解析 → 抽取 → 勾稽 → 报告 → 问答"全链路拆解为可独立验证的任务单元，每个任务都有明确的产物、依赖与验收标准。

### 0.2 任务编号规范

`M{milestone}.{seq}` —— 例：`M1.03` 表示 M1 阶段第 3 个任务。

### 0.3 任务字段说明

每个任务包含：
- **目标**：一句话说明产出
- **涉及文件**：新建 / 修改路径（相对仓库根）
- **依赖**：前置任务 ID
- **验收标准**：可验证的客观条件
- **验证方式**：如何确认完成

### 0.4 阶段时间分配

| 阶段 | 周次 | 工时 | 主要产出 |
|---|---|---|---|
| M1 | W1-2 | ~100h | 8 service docker-compose + 骨架 + mock 链路 |
| M2 | W3-4 | ~120h | 真实解析 + 三表抽取入库 |
| M3 | W5-6 | ~120h | 勾稽 + 异常 + 报告 PDF |
| M4 | W7-8 | ~140h | 3 个 LoRA adapter + 评估 |
| M5 | W9-10 | ~120h | ReAct 问答 + 知识库 |
| M6 | W11-12 | ~120h | 打磨 + 评估 + 发布 |

---

## 1. 仓库与目录结构

### 1.1 顶层目录

```
FinReport Agent/
├── docs/                      # 文档
│   ├── superpowers/
│   │   ├── specs/2026-07-13-finreport-agent-design.md
│   │   └── plans/2026-07-13-finreport-agent-implementation-plan.md
│   ├── design/                # 架构图、时序图源文件
│   ├── api/                   # OpenAPI 3.1 yaml
│   └── eval/                  # 各版本评估报告
├── backend/                   # L2 Java/SpringBoot
├── ai-service/                # L3 Python/FastAPI
├── frontend/                  # L1 Vue3
├── data/                      # 原始年报、训练数据
│   ├── raw_reports/
│   ├── training/
│   └── benchmark/
├── models/                    # LoRA adapter 本地缓存
├── scripts/                   # 一次性脚本：初始化、训练、评估
└── deploy/                    # docker-compose、Dockerfile、配置
```

### 1.2 backend/（L2 Java）

```
backend/
├── pom.xml
├── src/main/java/com/finreport/
│   ├── FinReportApplication.java
│   ├── config/                # WebFlux/Security/JWT/RabbitMQ/Redis/MinIO/Flyway/ReactiveTx 配置
│   ├── controller/            # REST + SSE Controller
│   ├── service/               # 业务服务
│   │   ├── orchestrator/      # M2 TaskOrchestrator
│   │   ├── sse/               # M3 SseEmitterPool
│   │   ├── file/              # M4 FileService
│   │   └── audit/             # M5 AuditLogger（待实现）
│   ├── mq/                    # RabbitMQ 生产者 + 消费者
│   ├── repository/            # R2DBC Repository
│   ├── domain/                # 领域对象
│   │   ├── dto/               # LoginRequest / TokenResponse 等
│   │   ├── entity/            # Report / Task / TaskStep / UserAccount
│   │   └── enums/             # TaskStatus / StepStatus / TaskStepName
│   ├── exception/             # GlobalExceptionHandler + 业务异常
│   ├── security/              # JWT filter / JwtUtil / UserDetailsService
│   ├── trace/                 # TraceContext / TraceIdWebFilter
│   └── util/                  # Idempotency / RateLimiter（待实现）
├── src/main/resources/
│   ├── application.yml
│   ├── db/migration/          # Flyway V1–V6 迁移
│   └── static/                # Swagger UI 资源
└── src/test/                  # JUnit5 + Testcontainers
```

### 1.3 ai-service/（L3 Python）

```
ai-service/
├── pyproject.toml
├── app/
│   ├── main.py                # FastAPI 入口
│   ├── api/                   # router：/internal/health、/internal/chat/stream 等
│   ├── core/                  # 配置、日志、 tracing
│   ├── mq/                    # RabbitMQ 消费者
│   ├── modules/
│   │   ├── parser/            # M6 DocumentParser
│   │   ├── extractor/         # M7 Extractor
│   │   ├── reasoner/          # M8 Reasoner
│   │   ├── agent/             # M9 AgentOrchestrator + tools
│   │   ├── generator/         # M10 ReportGenerator
│   │   └── modelhub/          # M11 ModelHub
│   ├── schemas/               # Pydantic 模型
│   └── utils/
├── tests/                     # pytest
└── Dockerfile
```

### 1.4 frontend/（L1 Vue3）

```
frontend/
├── package.json
├── vite.config.ts
├── tsconfig.json / tsconfig.app.json / tsconfig.node.json
├── .eslintrc.cjs
├── src/
│   ├── main.ts
│   ├── App.vue
│   ├── assets/                # 全局样式
│   ├── router/                # 路由
│   ├── stores/                # Pinia
│   ├── api/                   # axios + SSE client
│   ├── types/                 # TypeScript 类型定义
│   ├── views/                 # 页面
│   │   ├── Login.vue
│   │   ├── Reports.vue
│   │   ├── ReportUpload.vue
│   │   ├── TaskProgress.vue
│   │   ├── ReportDetail.vue   # 待实现（M2+）
│   │   └── Dashboard.vue      # 待实现（M6+）
│   ├── components/            # 通用组件
│   └── utils/
├── nginx.conf
└── Dockerfile
```

### 1.5 scripts/

```
scripts/
├── init_minio.py              # MinIO bucket 初始化
├── init_milvus.py             # Milvus collection 建立
├── declare_mq.py              # 声明 RabbitMQ exchange/queue
├── download_models.py         # 模型权重下载
├── download_sample.py         # 样例年报下载
├── inject_mock_progress.py    # E2E mock 进度注入
├── e2e_m116.py                # Playwright 浏览器 E2E
├── init_data.py               # 一键初始化（待实现，将整合上述脚本）
├── build_kb.py                # 离线知识库构建（待实现，M5）
├── crawl_cninfo.py            # 爬取巨潮年报（待实现，M4）
├── finreport-train            # 训练 CLI 入口（待实现，M4）
├── eval_extractor.py          # T1 评估（待实现，M4）
├── eval_embedder.py           # T2 评估（待实现，M4）
└── eval_layoutlm.py           # T3 评估（待实现，M4）
```

### 1.6 deploy/

```
deploy/
├── docker-compose.yml         # 10 个容器定义（9 常驻 + minio-init 退出）
├── docker-compose.dev.yml     # 开发覆盖（挂载源码）
├── .env.example               # 环境变量模板
├── prometheus/prometheus.yml  # M6 待实现
├── grafana/dashboards/        # M6 待实现
├── rabbitmq/definitions.json  # exchange/queue 声明（4 exchange + 6 queue）
├── rabbitmq/rabbitmq.conf     # RabbitMQ 配置
├── minio/init.sh              # MinIO bucket 初始化（容器启动时执行）
└── mysql/init.sql             # 数据库初始化
```

---

## 2. 通用约定

### 2.1 技术版本锁定

| 组件 | 版本 |
|---|---|
| Java | 21 |
| SpringBoot | 3.2.x |
| Python | 3.11 |
| PyTorch | 2.3.x + CUDA 12.1 |
| Vue | 3.4 |
| Element Plus | 2.7 |
| MySQL | 8.0 |
| Redis | 7.2 |
| Milvus | 2.4 |
| RabbitMQ | 3.13 |
| MinIO | 最新稳定版 |

### 2.2 代码规范

- Java：遵循 [Google Java Style](https://google.github.io/styleguide/javaguide.html)，4 空格缩进
- Python：black + ruff，行宽 100
- Vue：ESLint + Prettier
- 提交规范：Conventional Commits（`feat:`、`fix:`、`refactor:`、`docs:`、`test:`、`chore:`）

### 2.3 测试覆盖率门槛

- L2 Java 核心模块：≥ 80%
- L3 Python 核心模块：≥ 80%
- 前端：不强制，关键交互页需 E2E

### 2.4 CI 流水线

GitHub Actions 三条流水线：
1. `backend-ci`：lint + unit test + integration test（Testcontainers）
2. `ai-service-ci`：ruff + pytest + 模型 dry-run（小数据集）
3. `frontend-ci`：lint + build + Playwright smoke

---

## 3. M1 基础设施与骨架打通（Week 1-2）

### 3.1 阶段目标

搭好所有基础设施，跑通"上传 PDF → MQ → 落库 → SSE 回进度"骨架（L3 用 mock）。验收：上传任意 PDF → 前端能看到 mock 的 PARSE/EXTRACT/CHECK/REPORT 进度推送 → 数据库有 task 记录。

### 3.2 任务清单

#### M1.01 仓库初始化与目录骨架

- **目标**：建立顶层目录结构、初始化 git、添加 .gitignore / README 占位
- **涉及文件**：
  - `README.md`（占位）
  - `.gitignore`
  - `.editorconfig`
  - 各子目录占位 `.gitkeep`
- **依赖**：无
- **验收标准**：`git init` 完成；目录结构与 §1.1 一致
- **验证方式**：`tree -L 3` 检查

#### M1.02 Docker Compose 编排 8 service

- **目标**：`docker compose up` 一键拉起 frontend/backend/ai-service/mysql/redis/milvus/rabbitmq/minio
- **涉及文件**：
  - `deploy/docker-compose.yml`
  - `deploy/docker-compose.dev.yml`
  - `backend/Dockerfile`
  - `ai-service/Dockerfile`
  - `frontend/Dockerfile`
- **依赖**：M1.01
- **验收标准**：8 个容器全部 `healthy`；模型权重目录通过 volume 挂载
- **验证方式**：`docker compose ps` 全部 healthy；浏览器访问 `localhost:80` 前端、`localhost:8080` 后端、`localhost:15672` RabbitMQ 控制台

#### M1.03 MySQL 建表 + Flyway 迁移

- **目标**：实现设计文档 §5.2.2 的 12 张表
- **涉及文件**：
  - `backend/src/main/resources/db/migration/V1__init_user.sql`
  - `backend/src/main/resources/db/migration/V2__init_report.sql`
  - `backend/src/main/resources/db/migration/V3__init_task.sql`
  - `backend/src/main/resources/db/migration/V4__init_model_audit.sql`
  - `backend/src/main/resources/db/migration/V5__init_indexes.sql`
  - `backend/src/main/resources/db/migration/V6__m1_reliability_hardening.sql`（M1 可靠性加固）
- **依赖**：M1.02
- **验收标准**：Flyway 启动自动 migrate；表结构与 spec §5.2.2 一致；所有索引就位
- **验证方式**：`SHOW TABLES;` 返回 12 张表；`DESC financial_statement;` 字段匹配

#### M1.04 MinIO bucket 初始化

- **目标**：创建 spec §5.5.1 的 6 个 bucket
- **涉及文件**：
  - `scripts/init_minio.py`
  - `deploy/minio/init.sh`（容器启动后执行）
- **依赖**：M1.02
- **验收标准**：6 个 bucket 均存在；访问策略符合 §5.5.3
- **验证方式**：`mc ls minio/` 列出 6 个 bucket

#### M1.05 Milvus collection 建立

- **目标**：建立 `fin_kb` collection（spec §5.3）
- **涉及文件**：
  - `scripts/init_milvus.py`
- **依赖**：M1.02
- **验收标准**：collection schema 与 §5.3 一致；HNSW 索引参数正确
- **验证方式**：`python scripts/init_milvus.py` 后用 Attu UI 查看

#### M1.06 RabbitMQ 拓扑声明

- **目标**：声明 spec §3.1 的 4 个 exchange + 6 个 queue + DLQ
- **涉及文件**：
  - `scripts/declare_mq.py`
  - `deploy/rabbitmq/definitions.json`
- **依赖**：M1.02
- **验收标准**：所有 exchange/queue/dlq 存在；binding 正确；durable=true
- **验证方式**：RabbitMQ 管理界面查看拓扑；`scripts/declare_mq.py` 幂等执行

#### M1.07 L2 SpringBoot 骨架

- **目标**：WebFlux + Security + JWT + GlobalExceptionHandler + 健康检查
- **涉及文件**：
  - `backend/pom.xml`
  - `backend/src/main/java/com/finreport/FinReportApplication.java`
  - `backend/src/main/java/com/finreport/config/{WebFluxConfig,SecurityConfig,JwtConfig,JwtSecretValidator,RabbitMqConfig,RedisConfig,MinioConfig,FlywayMigrationConfiguration,ReactiveTransactionConfig}.java`
  - `backend/src/main/java/com/finreport/exception/GlobalExceptionHandler.java`
  - `backend/src/main/java/com/finreport/security/{JwtFilter,JwtUtil,UserDetailsServiceImpl}.java`
  - `backend/src/main/java/com/finreport/trace/{TraceContext,TraceIdWebFilter}.java`
  - `backend/src/main/resources/application.yml`
- **依赖**：M1.03、M1.04、M1.06
- **验收标准**：`/api/v1/system/health` 返回 200 + 各组件状态；未带 token 访问受保护接口返回 401
- **验证方式**：curl 健康检查；Postman 测登录获取 token 后访问 `/users/me`

#### M1.08 L2 AuthController（注册/登录/刷新/登出）

- **目标**：实现 spec §6.2.1 的 5 个端点
- **涉及文件**：
  - `backend/src/main/java/com/finreport/controller/AuthController.java`
  - `backend/src/main/java/com/finreport/service/AuthService.java`
  - `backend/src/main/java/com/finreport/domain/dto/{RegisterRequest,LoginRequest,TokenResponse}.java`
- **依赖**：M1.07
- **验收标准**：BCrypt 密码哈希；access 1h + refresh 7d；登出加入黑名单
- **验证方式**：JUnit5 测试 + Postman 流程

#### M1.09 L2 TaskOrchestrator 骨架

- **目标**：实现任务编排状态机（spec §2.2 M2），先用 mock L3
- **涉及文件**：
  - `backend/src/main/java/com/finreport/service/orchestrator/TaskOrchestrator.java`
  - `backend/src/main/java/com/finreport/service/orchestrator/TaskStateMachine.java`
  - `backend/src/main/java/com/finreport/mq/TaskMessageProducer.java`
  - `backend/src/main/java/com/finreport/domain/Task.java`
- **依赖**：M1.07
- **验收标准**：能创建 task → 发布 parse 消息 → 接收 progress → 推进状态
- **验证方式**：单元测试覆盖状态机所有合法转换

#### M1.10 L2 ProgressConsumer（消费 progress.exchange）

- **目标**：监听 `q.progress.results`，按 taskId 路由到 SseEmitterPool
- **涉及文件**：
  - `backend/src/main/java/com/finreport/mq/ProgressConsumer.java`
  - `backend/src/main/java/com/finreport/service/sse/SseEmitterPool.java`
- **依赖**：M1.06、M1.09
- **验收标准**：消费消息后正确路由；无活跃 SSE 时丢弃（不报错）
- **验证方式**：发条 mock progress 消息，对应的 SSE 连接收到事件

#### M1.11 L2 SSE 端点 /tasks/{id}/stream

- **目标**：实现 spec §6.3.2，含 Last-Event-ID 重连
- **涉及文件**：
  - `backend/src/main/java/com/finreport/controller/TaskController.java`
  - `backend/src/main/java/com/finreport/service/sse/SseService.java`
- **依赖**：M1.10
- **验收标准**：SSE 长连接保持；进度事件按 §6.3.2 格式；断线重连能恢复
- **验证方式**：浏览器 EventSource 测试断线重连

#### M1.12 L2 文件上传接口 /reports/upload

- **目标**：实现 spec §6.3.1，含分片上传到 MinIO、pdf_md5 计算、Idempotency-Key
- **涉及文件**：
  - `backend/src/main/java/com/finreport/controller/ReportController.java`
  - `backend/src/main/java/com/finreport/service/file/FileService.java`
  - `backend/src/main/java/com/finreport/util/IdempotencyChecker.java`
- **依赖**：M1.08、M1.09
- **验收标准**：上传 PDF → 返回 taskId + reportId；重复 Idempotency-Key 返回原 taskId；超大文件返回 413
- **验证方式**：curl 上传样例 PDF；断言 MinIO 中存在文件、MySQL 中存在 task 记录

#### M1.13 L3 FastAPI 骨架

- **目标**：FastAPI 入口 + /internal/health + mock /parse
- **涉及文件**：
  - `ai-service/pyproject.toml`
  - `ai-service/app/main.py`
  - `ai-service/app/api/health.py`
  - `ai-service/app/api/parse.py`（mock 返回假 Document）
- **依赖**：M1.02
- **验收标准**：`/internal/health` 返回 200；`/docs` Swagger UI 可访问
- **验证方式**：curl + 浏览器访问 `/docs`

#### M1.14 L3 RabbitMQ 消费者骨架

- **目标**：消费 q.parse.requests / q.extract.requests / q.reason.requests，调用 mock 处理函数后回报 progress
- **涉及文件**：
  - `ai-service/app/mq/consumer.py`
  - `ai-service/app/mq/producer.py`
  - `ai-service/app/modules/parser/handler.py`（mock）
  - `ai-service/app/modules/extractor/handler.py`（mock）
  - `ai-service/app/modules/reasoner/handler.py`（mock）
- **依赖**：M1.06、M1.13
- **验收标准**：prefetch=1；手动 ack；处理失败 nack(requeue=false) 进 DLQ；消息带 traceId 透传
- **验证方式**：发条 parse 消息 → L3 收到 → 回报 progress → L2 收到

#### M1.15 前端 Vue3 工程初始化

- **目标**：Vite + Vue3 + Pinia + Element Plus + axios + SSE client
- **涉及文件**：
  - `frontend/package.json`
  - `frontend/vite.config.ts`
  - `frontend/src/main.ts`
  - `frontend/src/router/index.ts`
  - `frontend/src/api/{http,sse}.ts`
- **依赖**：M1.02
- **验收标准**：`npm run dev` 启动；登录页可访问
- **验证方式**：浏览器访问 `localhost:5173`

#### M1.16 前端登录页 + 上传页 + 进度卡片

- **目标**：登录注册页、上传 PDF 页、进度卡片（4 阶段进度条）
- **涉及文件**：
  - `frontend/src/views/Login.vue`
  - `frontend/src/views/Reports.vue`
  - `frontend/src/views/ReportUpload.vue`
  - `frontend/src/components/ProgressCard.vue`
- **依赖**：M1.11、M1.12、M1.15
- **验收标准**：登录 → 上传 → SSE 进度更新可视化 → mock 链路全程跑通
- **验证方式**：人工操作 + 浏览器 DevTools 看 SSE 流

#### M1.17 CI 流水线搭建

- **目标**：GitHub Actions 三条流水线
- **涉及文件**：
  - `.github/workflows/backend-ci.yml`
  - `.github/workflows/ai-service-ci.yml`
  - `.github/workflows/frontend-ci.yml`
- **依赖**：M1.07、M1.13、M1.15
- **验收标准**：PR 触发 CI；lint + unit test 全绿
- **验证方式**：提交 PR 看 Actions 状态

### 3.3 M1 阶段验收

- [x] `docker compose up` 一键启动 9 个 healthy 容器（含 etcd）+ minio-init
- [x] 上传样例 PDF → 前端 SSE 看到 PARSE/EXTRACT/CHECK/REPORT 四阶段进度
- [x] MySQL 中存在 task + task_step 记录
- [x] MinIO 中存在原始 PDF
- [x] CI 三条流水线全绿（Backend CI / AI Service CI / Frontend CI）

> **M1 状态：✅ 已完成**（2026-07-19）。17 个任务全部完成。详见 [进度记录](../../progress/m1.md) 和 [决策记录](../../decisions/)。

---

## 4. M2 解析与抽取闭环（Week 3-4）

### 4.1 阶段目标

用真实模型替换 mock，能从 PDF 抽出三表数据。验收：上传 3 份不同格式年报，三表抽取 F1 ≥ 0.70（用 7B 通用 prompt，未微调）。

### 4.2 任务清单

#### M2.01 L3 M6 DocumentParser 实现

- **目标**：PyMuPDF 提取页面文本+图像（DPI=200）+ PP-StructureV2 版式分析
- **涉及文件**：
  - `ai-service/app/modules/parser/document_parser.py`
  - `ai-service/app/modules/parser/layout_analyzer.py`
  - `ai-service/app/schemas/document.py`（Page / TextBlock / TableBlock）
- **依赖**：M1.14
- **验收标准**：100 页 PDF 解析 < 90s；输出 Document 对象符合 spec §2.3 M6
- **验证方式**：用茅台 2024 年报测试，检查页面数、表格数

#### M2.02 L3 M6 表格识别（PP-Structure 内置）

- **目标**：调用 PP-Structure 表格还原，输出 HTML；LayoutLM 微调留到 M4
- **涉及文件**：
  - `ai-service/app/modules/parser/table_recognizer.py`
- **依赖**：M2.01
- **验收标准**：简单表格还原准确率 > 70%；输出 HTML 合法
- **验证方式**：抽样 20 个表格人工检查

#### M2.03 L3 M6 OCR 兜底

- **目标**：扫描件走 PaddleOCR
- **涉及文件**：
  - `ai-service/app/modules/parser/ocr_fallback.py`
- **依赖**：M2.01
- **验收标准**：扫描版 PDF 也能解析出文本；速度可接受
- **验证方式**：用扫描版年报测试

#### M2.04 L3 M11 ModelHub 加载 4-bit 7B

- **目标**：用 vLLM 或 llama.cpp 加载 Qwen2.5-7B-Instruct 4-bit，提供 generate() 接口
- **涉及文件**：
  - `ai-service/app/modules/modelhub/llm_loader.py`
  - `ai-service/app/modules/modelhub/modelhub.py`
- **依赖**：M1.13
- **验收标准**：6GB VRAM 下能加载；首 token < 5s；推理不 OOM
- **验证方式**：curl 调 `/internal/models/load` 后 `nvidia-smi` 看显存

#### M2.05 L3 M11 显存调度 + model_lock

- **目标**：Redis 分布式锁（spec §3.9），LRU 模型卸载
- **涉及文件**：
  - `ai-service/app/modules/modelhub/vram_scheduler.py`
  - `ai-service/app/core/redis_client.py`
- **依赖**：M2.04
- **验收标准**：多 worker 不会同时加载 7B；切换模型时正确 unload
- **验证方式**：起 2 个 worker 并发请求，检查只有 1 个加载 7B

#### M2.06 L3 M7 Extractor（7B 通用 prompt）

- **目标**：用 7B + JSON Schema prompt 抽取三表
- **涉及文件**：
  - `ai-service/app/modules/extractor/extractor.py`
  - `ai-service/app/modules/extractor/prompts.py`
  - `ai-service/app/schemas/statement.py`
- **依赖**：M2.02、M2.04
- **验收标准**：输出 JSON 符合 spec §2.3 M7 schema；JSON 解析率 > 90%
- **验证方式**：用 10 份年报测试，统计 JSON 解析率

#### M2.07 L3 M7 抽取结果校验

- **目标**：JSON Schema 校验 + 数值合法性 + 单位一致性
- **涉及文件**：
  - `ai-service/app/modules/extractor/validator.py`
  - `ai-service/app/schemas/statement_schema.json`
- **依赖**：M2.06
- **验收标准**：非法 JSON 能被捕获并触发重试（temperature=0.1）
- **验证方式**：构造非法输入测试

#### M2.08 L2 三表并行抽取编排

- **目标**：spec §3.2 的"三表并行"——同时发 3 条 extract 消息，等 3 条 progress 都到后触发 CHECK
- **涉及文件**：
  - `backend/src/main/java/com/finreport/service/orchestrator/ExtractDispatcher.java`
  - `backend/src/main/java/com/finreport/service/orchestrator/ExtractCompletionTracker.java`（基于 Redis AtomicInteger）
- **依赖**：M1.09、M1.10
- **验收标准**：3 条 extract 消息同时投递；CHECK 在 3 条都 SUCCESS 后才触发；任意 1 条 FAILED → 整任务 FAILED
- **验证方式**：日志检查 3 条 extract 并发执行；CHECK 不会提前触发

#### M2.09 L2 抽取结果写 financial_statement

- **目标**：消费 extract progress 后把 JSON 写入 financial_statement 表
- **涉及文件**：
  - `backend/src/main/java/com/finreport/service/StatementWriter.java`
  - `backend/src/main/java/com/finreport/repository/FinancialStatementRepository.java`
- **依赖**：M2.08
- **验收标准**：单份年报三表共 ~150 条科目记录入库；confidence 字段写入
- **验证方式**：`SELECT COUNT(*) FROM financial_statement WHERE report_id=?`

#### M2.10 抽取结果缓存

- **目标**：spec §3.10——同 pdf_md5 重传直接命中 Redis 缓存
- **涉及文件**：
  - `backend/src/main/java/com/finreport/service/ExtractCacheService.java`
- **依赖**：M2.09
- **验收标准**：重传同 PDF → 跳过 extract 步骤 → 直接 CHECK
- **验证方式**：上传相同 PDF 两次，第二次任务耗时显著降低

#### M2.11 前端三表展示页

- **目标**：可编辑表格组件展示资产负债表 / 利润表 / 现金流量表
- **涉及文件**：
  - `frontend/src/views/ReportDetail.vue`（Tab 结构）
  - `frontend/src/components/StatementTable.vue`
- **依赖**：M2.09、M1.16
- **验收标准**：三表分 Tab 展示；科目名、数值、单位、期间、scope 字段正确显示；可手动编辑数值（暂不写回）
- **验证方式**：上传年报 → 详情页查看三表

#### M2.12 集成测试：真实年报端到端

- **目标**：上传 3 份不同格式年报（茅台、平安、宁德），断言三表抽取 F1 ≥ 0.70
- **涉及文件**：
  - `backend/src/test/java/com/finreport/ReportParseIntegrationTest.java`
  - `data/benchmark/annual_reports/{moutai_2024,pingan_2024,catl_2024}.pdf`
  - `data/benchmark/ground_truth/{moutai_2024,pingan_2024,catl_2024}.json`
- **依赖**：M2.11
- **验收标准**：3 份年报抽取 F1 平均 ≥ 0.70
- **验证方式**：JUnit 集成测试通过

### 4.3 M2 阶段验收

- [ ] 上传茅台 / 平安 / 宁德 2024 年报，三表数据在前端可见
- [ ] 抽取 F1 ≥ 0.70
- [ ] 端到端耗时（PARSE+EXTRACT） < 3 min
- [ ] 重传相同 PDF 命中缓存

---

## 5. M3 勾稽异常与报告生成（Week 5-6）

### 5.1 阶段目标

完整财报解析链路可演示，含勾稽、异常、报告。验收：上传年报 → 4 分钟内出报告 → 勾稽准确率 ≥ 0.90。

### 5.2 任务清单

#### M3.01 L3 M8 Reasoner 勾稽规则引擎

- **目标**：实现 spec §2.3 M8 的 3 条勾稽规则（资产=负债+权益 / 净利润→未分配利润 / 经营现金流 vs 净利润）
- **涉及文件**：
  - `ai-service/app/modules/reasoner/accounting_rules.py`
  - `ai-service/app/modules/reasoner/rule_engine.py`
- **依赖**：M2.09
- **验收标准**：3 条规则全部能执行；通过/失败标记正确；diff 字段填充
- **验证方式**：用茅台年报跑，3 条规则应全部通过

#### M3.02 L3 M8 LLM 复核勾稽

- **目标**：硬编码规则失败时调用 7B 复核（可能是科目分类不同导致）
- **涉及文件**：
  - `ai-service/app/modules/reasoner/llm_reviewer.py`
- **依赖**：M3.01、M2.04
- **验收标准**：LLM 能解释差异原因；note 字段填充
- **验证方式**：构造一个故意不平衡的财报测试

#### M3.03 L3 M8 异常检测

- **目标**：同比/环比 > 30% 异常 + 科目逻辑异常（应收账款激增但营收下滑）
- **涉及文件**：
  - `ai-service/app/modules/reasoner/anomaly_detector.py`
- **依赖**：M3.01
- **验收标准**：能识别明显异常；severity 字段正确
- **验证方式**：构造测试用例（同科目本期 100 → 下期 200，应触发异常）

#### M3.04 L2 写 accounting_check + anomaly 表

- **目标**：消费 CHECK 完成消息后写表
- **涉及文件**：
  - `backend/src/main/java/com/finreport/service/CheckResultWriter.java`
  - `backend/src/main/java/com/finreport/repository/{AccountingCheckRepository,AnomalyRepository}.java`
- **依赖**：M3.01、M3.03
- **验收标准**：单份年报产生 3 条 check + N 条 anomaly 记录
- **验证方式**：SQL 查询验证

#### M3.05 L3 M10 ReportGenerator NLG

- **目标**：基于抽取结果 + 异常 + 检索段落，调用 7B 生成 5 段式报告
- **涉及文件**：
  - `ai-service/app/modules/generator/report_generator.py`
  - `ai-service/app/modules/generator/prompts.py`
- **依赖**：M2.04、M3.04
- **验收标准**：5 段（公司概况/财务概览/三表分析/异常与风险/结论）齐全；引用真实数据
- **验证方式**：人工检查报告内容合理性

#### M3.06 L3 M10 ECharts 服务端渲染

- **目标**：用 echarts-canvas-js 或 puppeteer 渲染 3 张图（资产结构饼图 / 营收趋势折线 / 现金流柱状）
- **涉及文件**：
  - `ai-service/app/modules/generator/chart_renderer.py`
  - `ai-service/app/modules/generator/chart_templates/`
- **依赖**：M3.05
- **验收标准**：3 张 PNG 生成；尺寸 800×500；数据正确
- **验证方式**：检查生成的 PNG 文件

#### M3.07 L3 M10 Markdown → PDF

- **目标**：用 WeasyPrint 把 Markdown + 图表转 PDF
- **涉及文件**：
  - `ai-service/app/modules/generator/pdf_converter.py`
  - `ai-service/app/modules/generator/templates/report.html`（Jinja2）
- **依赖**：M3.06
- **验收标准**：PDF 包含 5 段文字 + 3 张图表；排版正常
- **验证方式**：打开生成的 PDF 检查

#### M3.08 L2 写 report_artifact + 上传 MinIO

- **目标**：消费 REPORT 完成消息后把 PDF/MD/PNG 上传 MinIO，写 report_artifact 表
- **涉及文件**：
  - `backend/src/main/java/com/finreport/service/ReportArtifactWriter.java`
  - `backend/src/main/java/com/finreport/repository/ReportArtifactRepository.java`
- **依赖**：M3.07
- **验收标准**：MinIO `reports/{reportId}/` 下有 report.pdf、report.md、charts/*.png
- **验证方式**：mc ls 检查；预签名 URL 可下载

#### M3.09 前端勾稽页 + 异常页 + 报告页

- **目标**：详情页新增 3 个 Tab
- **涉及文件**：
  - `frontend/src/components/CheckList.vue`
  - `frontend/src/components/AnomalyList.vue`
  - `frontend/src/components/ReportViewer.vue`（Markdown 渲染 + 图表展示 + PDF 下载）
- **依赖**：M3.04、M3.08、M2.11
- **验收标准**：勾稽页显示规则列表 + 通过/失败标识；异常页按严重度排序卡片；报告页 Markdown 渲染 + 图表 + PDF 下载按钮
- **验证方式**：人工浏览详情页

#### M3.10 端到端 SLA 测试

- **目标**：验证 spec §3.7 的 SLA 目标
- **涉及文件**：
  - `backend/src/test/java/com/finreport/SlaIntegrationTest.java`
- **依赖**：M3.08、M3.09
- **验收标准**：PARSE < 90s、EXTRACT < 60s、CHECK < 30s、REPORT < 45s、总链路 < 4min
- **验证方式**：JUnit 测试用 Stopwatch 断言

### 5.3 M3 阶段验收

- [ ] 上传茅台年报 → 4 分钟内详情页所有 Tab 数据齐全
- [ ] 勾稽准确率 ≥ 0.90（10 份测试年报）
- [ ] 报告 PDF 可下载且内容合理
- [ ] 异常列表至少识别出 1 个真实异常（或 0 异常时明确说明）

---

## 6. M4 模型微调 T1/T2/T3（Week 7-8）

### 6.1 阶段目标

训练 3 个微调模型并替换通用模型，效果提升。验收：T1 F1 ≥ 0.85、T2 MRR ≥ 0.78、T3 mAP ≥ 0.88、端到端抽取 F1 ≥ 0.85。

### 6.2 任务清单

#### M4.01 数据采集：爬取 150 份 A 股年报

- **目标**：从巨潮资讯网爬取 10 行业 × 5 公司 × 3 年 = 150 份年报 PDF
- **涉及文件**：
  - `scripts/crawl_cninfo.py`
  - `data/raw_reports/{industry}/{company}_{year}.pdf`
- **依赖**：无
- **验收标准**：150 份 PDF 下载完成；行业分布均匀
- **验证方式**：`ls data/raw_reports/*/*.pdf | wc -l` = 150

#### M4.02 T1 自研 Web 标注页

- **目标**：Vue 简单页：左边显示表格 HTML，右边填 JSON，提交存 MinIO
- **涉及文件**：
  - `frontend/src/views/admin/Annotation.vue`
  - `backend/src/main/java/com/finreport/controller/AnnotationController.java`
  - `backend/src/main/java/com/finreport/service/AnnotationService.java`
- **依赖**：M4.01
- **验收标准**：能加载 PDF 表格 → 编辑 JSON → 提交存储
- **验证方式**：标注 1 条样本走通流程

#### M4.03 T1 数据标注 5k 样本

- **目标**：人工 + 半自动（PP-Structure 预填 + 人工校正）标注 5000 条
- **涉及文件**：
  - `data/training/extractor/samples.jsonl`
- **依赖**：M4.02
- **验收标准**：5000 条样本；训练/验证/测试切分 4500/300/200
- **验证方式**：`wc -l data/training/extractor/samples.jsonl` = 5000

#### M4.04 T1 训练脚本 QLoRA

- **目标**：实现 spec §4.2.3 训练配置
- **涉及文件**：
  - `scripts/finreport-train`（CLI 入口）
  - `scripts/train_extractor.py`
  - `ai-service/app/modules/modelhub/trainer/extractor_trainer.py`
- **依赖**：M4.03
- **验收标准**：3 epoch 跑完不 OOM；显存峰值 ≤ 5.2GB；产出 LoRA adapter
- **验证方式**：`finreport-train train-extractor --version v1.0.0`；训练日志监控

#### M4.05 T1 评估

- **目标**：JSON 解析率、字段 F1、数值准确率，对比基线
- **涉及文件**：
  - `scripts/eval_extractor.py`
- **依赖**：M4.04
- **验收标准**：JSON 解析率 ≥ 95%；字段 F1 ≥ 0.85；数值准确率 ≥ 0.90
- **验证方式**：`finreport-train eval-extractor --version v1.0.0` 输出报告

#### M4.06 T2 embedding 数据构造

- **目标**：20k 正负对（同科目不同表述、段落摘要、问答对、困难负例）
- **涉及文件**：
  - `scripts/build_embedder_pairs.py`
  - `data/training/embedder/pairs.jsonl`
- **依赖**：M4.01
- **验收标准**：20k 正对 + 60k 负例；格式 (query, positive, [negatives])
- **验证方式**：抽样人工检查质量

#### M4.07 T2 训练 bge-small-zh LoRA

- **目标**：实现 spec §4.3.3，InfoNCE + in-batch negatives
- **涉及文件**：
  - `scripts/train_embedder.py`
  - `ai-service/app/modules/modelhub/trainer/embedder_trainer.py`
- **依赖**：M4.06
- **验收标准**：5 epoch 跑完；显存峰值 ≤ 3.8GB
- **验证方式**：训练日志

#### M4.08 T2 评估

- **目标**：MRR@10、Recall@5
- **涉及文件**：
  - `scripts/eval_embedder.py`
- **依赖**：M4.07
- **验收标准**：MRR@10 ≥ 0.78；Recall@5 ≥ 0.85
- **验证方式**：`finreport-train eval-embedder`

#### M4.09 T3 Label Studio 表格标注

- **目标**：用 Label Studio 标注 2000 个表格（cell bbox + 角色）
- **涉及文件**：
  - `deploy/label-studio/docker-compose.yml`
  - `data/training/layoutlm/`
- **依赖**：M4.01
- **验收标准**：2000 张表格图标注完成；Label Studio 导出 COCO 格式
- **验证方式**：抽样人工校对

#### M4.10 T3 训练 LayoutLMv3

- **目标**：实现 spec §4.4.4，冻结前 8 层 + 训练后 4 层 + 头
- **涉及文件**：
  - `scripts/train_layoutlm.py`
  - `ai-service/app/modules/modelhub/trainer/layoutlm_trainer.py`
- **依赖**：M4.09
- **验收标准**：5000 steps 跑完；显存峰值 ≤ 4.5GB
- **验证方式**：训练日志

#### M4.11 T3 评估

- **目标**：Cell mAP@0.5、角色分类 F1、TEDS
- **涉及文件**：
  - `scripts/eval_layoutlm.py`
- **依赖**：M4.10
- **验收标准**：mAP ≥ 0.88；F1 ≥ 0.85；TEDS ≥ 0.82
- **验证方式**：`finreport-train eval-layoutlm`

#### M4.12 ModelHub 动态 LoRA 加载

- **目标**：ModelHub 启动加载 production adapter；推理时 PEFT 动态加载
- **涉及文件**：
  - `ai-service/app/modules/modelhub/adapter_loader.py`
  - 修改 `ai-service/app/modules/modelhub/modelhub.py`
- **依赖**：M4.05、M4.08、M4.11
- **验收标准**：3 个 adapter 都能加载；切换无 OOM
- **验证方式**：调用 `/internal/models/load?task=extractor` 后推理

#### M4.13 model_registry 注册与版本管理

- **目标**：训练完自动注册到 model_registry 表；candidate → staged → production 流转
- **涉及文件**：
  - `backend/src/main/java/com/finreport/service/ModelRegistryService.java`
  - `backend/src/main/java/com/finreport/controller/admin/ModelController.java`
  - 修改训练脚本：训练完自动调 L2 注册接口
- **依赖**：M4.12
- **验收标准**：3 个 adapter 在 model_registry 有记录；status 流转可控
- **验证方式**：`SELECT * FROM model_registry` 查询

#### M4.14 A/B 对比：微调前后效果

- **目标**：用同一测试集跑微调前/后，输出对比报告
- **涉及文件**：
  - `scripts/ab_compare.py`
  - `docs/eval/m4_finetune_compare.md`
- **依赖**：M4.13
- **验收标准**：抽取 F1 提升 ≥ 0.15（0.70 → 0.85）
- **验证方式**：报告文档

#### M4.15 替换主链路使用微调模型

- **目标**：M7 Extractor 用 1.5B QLoRA 替换 7B；M6 表格识别用 LayoutLM；M9 search_kb 用微调 embedder
- **涉及文件**：
  - 修改 `ai-service/app/modules/extractor/extractor.py`（路由到 1.5B）
  - 修改 `ai-service/app/modules/parser/table_recognizer.py`（路由到 LayoutLM）
- **依赖**：M4.14
- **验收标准**：端到端抽取 F1 ≥ 0.85；耗时显著降低（1.5B 比 7B 快）
- **验证方式**：M2 集成测试重跑

### 6.3 M4 阶段验收

- [ ] 3 个 LoRA adapter 训练完成并注册
- [ ] T1 F1 ≥ 0.85、T2 MRR ≥ 0.78、T3 mAP ≥ 0.88
- [ ] 端到端抽取 F1 ≥ 0.85
- [ ] A/B 对比报告产出
- [ ] 主链路已切换到微调模型

---

## 7. M5 Agent 问答与知识库（Week 9-10）

### 7.1 阶段目标

ReAct 问答可用，知识库检索增强。验收：5 个标准问题关键事实命中率 ≥ 0.85；首 token < 15s。

### 7.2 任务清单

#### M5.01 L3 M9 AgentOrchestrator ReAct 循环

- **目标**：实现 Thought → Action → Observation → Thought... → Final Answer
- **涉及文件**：
  - `ai-service/app/modules/agent/orchestrator.py`
  - `ai-service/app/modules/agent/react_parser.py`
  - `ai-service/app/modules/agent/prompts.py`
- **依赖**：M2.04
- **验收标准**：能解析 LLM 输出的 tool_call；步数上限 8；tool 报错 3 次终止
- **验证方式**：单元测试覆盖各种 LLM 输出格式

#### M5.02 L3 M9 ToolRegistry + 6 个工具实现

- **目标**：实现 spec §2.3 M9 工具表
- **涉及文件**：
  - `ai-service/app/modules/agent/tools/{query_statement,compute_yoy,compute_qoq,check_accounting,search_kb,unit_convert}.py`
  - `ai-service/app/modules/agent/tool_registry.py`
- **依赖**：M5.01、M2.09、M3.04
- **验收标准**：6 个工具均能执行；返回 JSON 结构稳定
- **验证方式**：每个工具单测

#### M5.03 L3 M9 JSON Schema 强约束

- **目标**：用 outlines / guidance 库约束 LLM 输出 JSON
- **涉及文件**：
  - `ai-service/app/modules/agent/json_constraint.py`
- **依赖**：M5.01
- **验收标准**：tool_call 解析失败率 < 5%
- **验证方式**：100 次推理测试

#### M5.04 L3 M9 内部 SSE 端点 /internal/chat/stream

- **目标**：M9 推理时通过 HTTP chunked 推 token 给 L2（spec §3.3）
- **涉及文件**：
  - `ai-service/app/api/chat.py`
- **依赖**：M5.01
- **验收标准**：SSE 流式 token；thought/tool_call/tool_result 事件结构化输出
- **验证方式**：curl 调用看流式输出

#### M5.05 L2 问答 SSE 透传

- **目标**：L2 发 chat 消息 + 建立 SSE 拉流，token 透传前端
- **涉及文件**：
  - `backend/src/main/java/com/finreport/controller/ChatController.java`
  - `backend/src/main/java/com/finreport/service/ChatService.java`
  - `backend/src/main/java/com/finreport/mq/ChatMessageProducer.java`
  - `backend/src/main/java/com/finreport/service/sse/ChatStreamProxy.java`（WebClient 拉流）
- **依赖**：M5.04
- **验收标准**：前端 SSE 收到 token + thought + tool_call 事件；chat_done 时关闭 SSE
- **验证方式**：浏览器 EventSource 测试

#### M5.06 L2 会话上下文管理

- **目标**：Redis Hash 存最近 10 轮；超长触发 LLM 摘要压缩
- **涉及文件**：
  - `backend/src/main/java/com/finreport/service/SessionContextService.java`
- **依赖**：M5.05
- **验收标准**：第 11 轮触发压缩；压缩后保留关键事实
- **验证方式**：构造 11 轮对话测试

#### M5.07 L3 知识库构建脚本

- **目标**：spec §3.4 build_kb.py 批量入库
- **涉及文件**：
  - `scripts/build_kb.py`
  - `ai-service/app/modules/parser/chunker.py`（chunk_size=512, overlap=64）
- **依赖**：M4.08（用微调 embedder）
- **验收标准**：50 份年报入库 Milvus；chunk 数 ~5000
- **验证方式**：Attu UI 查看 collection 行数

#### M5.08 前端问答页

- **目标**：消息流 + thought/tool_call 折叠面板 + Markdown 渲染 + 工具调用高亮
- **涉及文件**：
  - `frontend/src/views/Chat.vue`
  - `frontend/src/components/ChatMessage.vue`
  - `frontend/src/components/ToolCallPanel.vue`
- **依赖**：M5.05
- **验收标准**：SSE 事件正确渲染；折叠面板可展开；Markdown 表格/列表正常
- **验证方式**：人工测试

#### M5.09 问答链路集成测试

- **目标**：5 个标准问题测试
- **涉及文件**：
  - `backend/src/test/java/com/finreport/ChatIntegrationTest.java`
  - `data/benchmark/qa_questions.json`
- **依赖**：M5.08
- **验收标准**：关键事实命中率 ≥ 0.85；首 token < 15s
- **验证方式**：JUnit 集成测试

### 7.3 M5 阶段验收

- [ ] 5 个标准问题命中率 ≥ 0.85
- [ ] 首 token < 15s
- [ ] ReAct thought/tool_call 可视化正常
- [ ] 知识库预置 50 份年报

---

## 8. M6 前端打磨评估与发布（Week 11-12）

### 8.1 阶段目标

可对外演示的 Demo。验收：所有 §7.4.2 评估指标达标；一键 `docker compose up` 可启动；演示视频完成。

### 8.2 任务清单

#### M6.01 前端 UI 打磨

- **目标**：响应式布局、加载态/空态/错误态、暗黑模式
- **涉及文件**：
  - 修改 `frontend/src/views/*.vue`
  - 修改 `frontend/src/components/*.vue`
  - `frontend/src/styles/theme.scss`
- **依赖**：M5.08
- **验收标准**：3 种屏幕尺寸（手机/平板/桌面）布局正常；所有异步操作有加载态
- **验证方式**：Chrome DevTools 多设备测试

#### M6.02 前端性能优化

- **目标**：路由懒加载、组件缓存、SSE 背压处理
- **涉及文件**：
  - 修改 `frontend/src/router/index.ts`
  - 修改 `frontend/src/api/sse.ts`（背压 buffer 64）
- **依赖**：M6.01
- **验收标准**：首屏加载 < 2s；SSE 高频更新不卡顿
- **验证方式**：Lighthouse 评分 ≥ 90

#### M6.03 L2 限流完善

- **目标**：实现 spec §6.7 限流策略
- **涉及文件**：
  - `backend/src/main/java/com/finreport/util/RateLimiter.java`
  - 修改各 Controller 添加 @RateLimit 注解
- **依赖**：M3.10
- **验收标准**：超限返回 429 + Retry-After
- **验证方式**：压测脚本验证

#### M6.04 L2 Idempotency 完善

- **目标**：所有写操作要求 Idempotency-Key
- **涉及文件**：
  - `backend/src/main/java/com/finreport/util/IdempotencyChecker.java`
  - 修改写接口添加 @Idempotent 注解
- **依赖**：M6.03
- **验收标准**：重复请求返回原结果
- **验证方式**：集成测试

#### M6.05 可观测性：Prometheus 指标

- **目标**：实现 spec §7.2.3 核心指标
- **涉及文件**：
  - `backend/src/main/java/com/finreport/metrics/{BusinessMetrics,SystemMetrics}.java`
  - `ai-service/app/core/metrics.py`
  - `deploy/prometheus/prometheus.yml`
- **依赖**：M3.10
- **验收标准**：`/api/v1/system/metrics` 返回 Prometheus 格式；包含所有 §7.2.3 指标
- **验证方式**：Prometheus targets up

#### M6.06 Grafana 看板

- **目标**：3 个看板：业务、系统、模型
- **涉及文件**：
  - `deploy/grafana/dashboards/{business,system,model}.json`
  - `deploy/grafana/provisioning/`
- **依赖**：M6.05
- **验收标准**：3 个看板可视化所有指标
- **验证方式**：Grafana UI 查看

#### M6.07 Jaeger 链路追踪

- **目标**：spec §7.2.4，traceId 跨服务传递
- **涉及文件**：
  - `backend/src/main/java/com/finreport/config/TracingConfig.java`
  - `ai-service/app/core/tracing.py`
  - 修改 MQ 消息属性传递 traceId
- **依赖**：M6.05
- **验收标准**：Jaeger UI 能看到完整链路
- **验证方式**：上传 PDF → Jaeger 查询 traceId

#### M6.08 端到端评估：30 份基准数据集

- **目标**：spec §7.4.1 的 30 份年报 + ground truth
- **涉及文件**：
  - `data/benchmark/annual_reports/`（30 份）
  - `data/benchmark/ground_truth/`（30 份 JSON）
  - `scripts/eval_e2e.py`
- **依赖**：M5.09
- **验收标准**：所有 §7.4.2 指标达标
- **验证方式**：`python scripts/eval_e2e.py` 输出报告

#### M6.09 评估报告产出

- **目标**：docs/eval/v1.0.md
- **涉及文件**：
  - `docs/eval/v1.0.md`
- **依赖**：M6.08
- **验收标准**：报告含所有指标 + 与上版本对比 + 改进点
- **验证方式**：人工审阅

#### M6.10 README + 部署文档

- **目标**：项目 README + 一键部署文档 + API 文档导出
- **涉及文件**：
  - `README.md`（含项目介绍、架构图、快速开始、演示链接）
  - `docs/deployment.md`
  - `docs/api/openapi.yaml`（springdoc 导出）
- **依赖**：M6.09
- **验收标准**：陌生人按 README 能跑起来
- **验证方式**：找朋友按文档部署

#### M6.11 Docker Compose 一键启动验证

- **目标**：干净环境 `docker compose up` 全栈起来
- **涉及文件**：
  - 修改 `deploy/docker-compose.yml`
  - `deploy/init.sh`（一键初始化数据）
- **依赖**：M6.10
- **验收标准**：从零启动 < 5min；8 service healthy
- **验证方式**：在干净 VM 上测试

#### M6.12 演示视频脚本 + 录制

- **目标**：5-10 分钟演示视频
- **涉及文件**：
  - `docs/demo_script.md`
- **依赖**：M6.11
- **验收标准**：视频覆盖 5 大能力（上传/三表/勾稽/报告/问答）
- **验证方式**：视频录制完成

#### M6.13 项目总结博客

- **目标**：技术博客，重点讲架构决策 + 微调经验 + 踩坑
- **涉及文件**：
  - `docs/blog/v1.0-summary.md`
- **依赖**：M6.12
- **验收标准**：≥ 3000 字；含架构图、性能数据、对比表
- **验证方式**：人工审阅

### 8.3 M6 阶段验收

- [ ] 所有 §7.4.2 评估指标达标
- [ ] `docker compose up` 一键启动
- [ ] 演示视频完成
- [ ] README + 部署文档齐全
- [ ] 项目博客发布

---

## 9. 跨阶段并行与依赖

### 9.1 关键路径

```
M1 (基础设施) ── M2 (解析抽取) ── M3 (勾稽报告) ── M6 (打磨发布)
                      │
                      └── M4 (微调) ── M5 (问答) ──┘
```

### 9.2 可并行任务

| 主线 | 并行任务 |
|---|---|
| M2 解析抽取 | M4.01-02 数据采集 + 标注页开发 |
| M2 末期 | M4.03 T1 数据标注（开发并行） |
| M3 勾稽报告 | M4.06 T2 数据构造、M4.09 T3 标注 |
| M3 末期 | M5.01-03 ReAct 框架（不依赖 M4） |
| M4 训练期 | M5.04-06 L2 问答骨架（用 7B 暂替） |

### 9.3 关键依赖锁定

- M5.07 知识库构建依赖 M4.08（微调 embedder）
- M5.02 search_kb 工具依赖 M5.07（知识库就绪）
- M4.15 替换主链路依赖 M4.13（model_registry 就绪）
- M6.08 端到端评估依赖 M5.09（问答就绪）

---

## 10. 风险与缓冲

### 10.1 风险跟踪表

| 风险 ID | 描述 | 概率 | 影响 | 缓冲策略 | 触发条件 |
|---|---|---|---|---|---|
| R1 | 6GB VRAM 训练 OOM | 高 | 训练中断 | M1 末做 dry run；备选 8-bit 量化 | 训练日志 OOM |
| R2 | T1 微调效果不达 0.85 | 中 | 抽取质量低 | 预留 1 周增数据；回退 7B | M4.05 评估 |
| R3 | LayoutLM 标注耗时超预期 | 中 | M4 延期 | M2 开始启动标注；用 PP-Structure 预填 | M4.09 进度 |
| R4 | 端到端 SLA 不达 4min | 中 | 用户体验差 | M3 末性能 profiling；优化 model_lock | M3.10 测试 |
| R5 | RabbitMQ 消息积压 | 低 | 任务卡死 | prefetch=1 + 监控告警 + DLQ | DLQ 长度 > 10 |
| R6 | PDF 解析失败率高 | 中 | 用户流失 | OCR 兜底 + 预处理清洗 | M2.12 测试 |
| R7 | Milvus 单机性能不足 | 低 | 检索慢 | HNSW 参数调优；可切 Qdrant | M5.07 入库慢 |

### 10.2 缓冲时间

- M4 末预留 3 天缓冲（应对 R1/R2）
- M6 末预留 3 天缓冲（应对评估指标不达标）
- 每周末预留半天处理本周遗留 issue

---

## 11. Definition of Done

### 11.1 任务级 DoD

每个任务完成需满足：
1. 代码已提交（Conventional Commits）
2. 单元测试已编写并通过（覆盖率 ≥ 80%）
3. 集成测试（如适用）通过
4. 文档（API/README 章节）已更新
5. CI 流水线全绿

### 11.2 里程碑级 DoD

每个里程碑完成需满足：
1. 所有任务级 DoD 满足
2. 阶段验收标准全部达成
3. 阶段总结写入 `docs/progress/m{N}.md`
4. 演示视频片段录制（M1-M5 各 1-2 分钟）
5. 设计文档与实现如有偏差，更新 spec

### 11.3 项目级 DoD（v1.0 发布）

1. 6 个里程碑全部完成
2. 所有 §7.4.2 评估指标达标
3. `docker compose up` 一键启动
4. 30 份基准数据集端到端评估报告产出
5. 演示视频（5-10 分钟）完成
6. README + 部署文档 + API 文档齐全
7. 项目博客发布
8. 所有代码、模型、数据归档到 MinIO `finreport-backups/`

---

## 附录：任务统计

| 阶段 | 任务数 | 关键产出 |
|---|---|---|
| M1 | 17 | 基础设施 + 骨架 |
| M2 | 12 | 解析抽取闭环 |
| M3 | 10 | 勾稽报告生成 |
| M4 | 15 | 3 个微调模型 |
| M5 | 9 | Agent 问答 + KB |
| M6 | 13 | 打磨评估发布 |
| **合计** | **76** | **完整 Demo** |
