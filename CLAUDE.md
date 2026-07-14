# CLAUDE.md — FinReport Agent 开发规范

> 本文件为 AI 协作开发规范，所有 AI 助手（Claude Code / Codex 等）在本仓库工作前必须完整阅读并严格遵守。
> 配套文档：
> - 设计文档：docs/superpowers/specs/2026-07-13-finreport-agent-design.md
> - 实现计划：docs/superpowers/plans/2026-07-13-finreport-agent-implementation-plan.md

---

## 0. 项目速览

- 项目名称：FinReport Agent — A 股上市公司财报深度解析 Agent
- 核心能力：PDF 财报 → 结构化科目 → 勾稽核对 → 异常检测 → NLG 报告 → ReAct 问答
- 架构：5 层（L1 Vue3 / L2 SpringBoot / L3 FastAPI / L4 Models / L5 Data）
- 关键约束：RTX 4050 Mobile 6GB VRAM，必须使用 4-bit 量化 + QLoRA
- 总周期：12 周 / 6 里程碑 / 76 任务

---

## 1. 技术栈版本（严格锁定）

| 组件 | 版本 | 说明 |
|---|---|---|
| Java | 17 | LTS |
| SpringBoot | 3.2.x | WebFlux + Security |
| Python | 3.11 | AI 服务 |
| PyTorch | 2.3.x + CUDA 12.1 | 必须 4-bit 量化 |
| Vue | 3.4 | Composition API |
| Element Plus | 2.7 | UI 库 |
| MySQL | 8.0 | 主数据库 |
| Redis | 7.2 | 缓存 + 分布式锁 |
| Milvus | 2.4 | 向量库 |
| RabbitMQ | 3.13 | 消息队列 |
| MinIO | 最新稳定版 | 对象存储 |

禁止擅自升级版本，需要升级时先在 spec 中提出变更申请。

---

## 2. 仓库目录约定

    FinReport Agent/
    ├── docs/                  # 文档（spec / plan / api / eval / blog）
    ├── backend/               # L2 Java/SpringBoot
    ├── ai-service/            # L3 Python/FastAPI
    ├── frontend/              # L1 Vue3
    ├── data/                  # 原始数据 + 训练数据 + 基准数据
    ├── models/                # LoRA adapter 本地缓存（不入库）
    ├── scripts/               # 一次性脚本：初始化、训练、评估
    ├── deploy/                # docker-compose、Dockerfile、配置
    └── CLAUDE.md              # 本文件

### 2.1 模块对应关系

| 设计文档模块 | 实现位置 |
|---|---|
| M1 接入网关 | backend/.../controller/AuthController.java |
| M2 任务编排 | backend/.../service/orchestrator/ |
| M3 会话与 SSE | backend/.../service/sse/ |
| M4 文件与产物 | backend/.../service/file/ |
| M5 审计与配置 | backend/.../service/audit/ |
| M6 文档解析 | ai-service/app/modules/parser/ |
| M7 科目抽取 | ai-service/app/modules/extractor/ |
| M8 勾稽与异常 | ai-service/app/modules/reasoner/ |
| M9 Agent 编排 | ai-service/app/modules/agent/ |
| M10 报告生成 | ai-service/app/modules/generator/ |
| M11 模型与训练 | ai-service/app/modules/modelhub/ |

---

## 3. 代码规范

### 3.1 Java（L2）

- 风格：Google Java Style，4 空格缩进
- 包名：com.finreport.{layer}.{module}
- 类命名：UpperCamelCase
- 方法命名：lowerCamelCase
- 常量：UPPER_SNAKE_CASE
- 必须：
  - 所有 public 方法有 Javadoc
  - Service 层方法首行用 logger 记录入参
  - 异常抛出用项目内 BusinessException 子类，不直接抛 RuntimeException
  - DTO 用 record（Java 17）
- 禁止：
  - 在 Controller 写业务逻辑
  - 直接 printStackTrace
  - 魔法数字（用常量或枚举）

### 3.2 Python（L3）

- 风格：black + ruff，行宽 100
- 导入顺序：标准库 → 第三方 → 本项目
- 类型注解：所有函数签名必须带类型注解
- 必须：
  - Pydantic 模型定义在 app/schemas/
  - 日志用项目封装的 logger，禁止 print
  - 异步函数用 async def
- 禁止：
  - 在业务代码 print
  - 直接 import transformers（必须走 ModelHub）
  - 全局变量（用 app.core.config.Settings）

### 3.3 Vue（L1）

- 风格：ESLint + Prettier，2 空格缩进
- 组件：script setup + lang ts
- 命名：组件 PascalCase，props/events camelCase
- 必须：
  - API 调用统一走 src/api/
  - 状态管理用 Pinia store
  - SSE 客户端封装在 src/api/sse.ts
- 禁止：
  - 在组件内直接 fetch
  - 修改 props
  - 用 any 类型

### 3.4 SQL

- 关键字大写：SELECT * FROM user WHERE id = 1
- 表名小写下划线：financial_statement
- 字段名小写下划线：item_name
- 索引命名：idx_{table}_{col}，唯一索引：uk_{table}_{col}
- 所有表必须有 created_at，更新表加 updated_at

---

## 4. 提交规范（Conventional Commits）

### 4.1 格式

    <type>(<scope>): <subject>

    <body>

    <footer>

### 4.2 type 列表

- feat: 新功能
- fix: Bug 修复
- refactor: 重构（不改行为）
- perf: 性能优化
- docs: 文档
- test: 测试
- chore: 构建/工具
- ci: CI 配置

### 4.3 scope 列表

按模块：auth / orchestrator / sse / parser / extractor / reasoner / agent / generator / modelhub / frontend / deploy / docs

### 4.4 示例

    feat(extractor): 实现 7B 通用 prompt 三表抽取

    - 新增 Extractor 类，路由到 ModelHub.generate
    - 添加 JSON Schema 校验与重试逻辑
    - 关联任务：M2.06

    Refs: M2.06

### 4.5 提交粒度

- 一个任务一个 PR（如 M1.07 一个 PR）
- 单次 commit 不超过 500 行变更
- 禁止 git push --force 到 main 分支

---

## 5. 分支策略

- main: 生产分支，受保护
- develop: 集成分支
- feature/{task-id}-{short-desc}: 功能分支
- fix/{short-desc}: 修复分支
- release/v1.0: 发布分支

- feature 命名示例：feature/M1.07-springboot-skeleton
- PR 必须通过 CI + 至少 1 个 review
- 合并用 squash merge

---

## 6. 测试要求

### 6.1 覆盖率门槛

- L2 Java 核心模块：覆盖率 >= 80%，工具 JUnit5 + Mockito + JaCoCo
- L3 Python 核心模块：覆盖率 >= 80%，工具 pytest + coverage
- L1 前端：不强制覆盖率，关键交互需 Playwright E2E

### 6.2 测试类型分布

- 单元测试 70%: 快速、隔离、mock 依赖
- 集成测试 25%: Testcontainers 启动真实容器
- E2E 测试 5%: Playwright 端到端

### 6.3 测试命名

- Java: 类名以 Test 结尾，方法 should{Expected}When{Condition}
- Python: test_{action}_{condition}_{expected}

### 6.4 必测场景

- 任务状态机所有合法转换
- JSON Schema 校验（合法 + 非法输入）
- 勾稽规则（通过 + 失败 + 边界）
- MQ 死信流转
- SSE 断线重连
- 模型降级（mock 7B 超时 → 切 API）
- 限流触发

---

## 7. 常用命令

### 7.1 启动开发环境

    # 一键启动所有依赖（MySQL/Redis/Milvus/RabbitMQ/MinIO）
    cd deploy
    docker compose -f docker-compose.dev.yml up -d

    # 启动后端
    cd backend
    ./mvnw spring-boot:run

    # 启动 AI 服务
    cd ai-service
    uvicorn app.main:app --reload --port 8000

    # 启动前端
    cd frontend
    npm run dev

### 7.2 初始化数据

    # 建表 + bucket + collection + MQ 拓扑
    python scripts/init_data.py

    # 预置 50 份年报到知识库
    python scripts/build_kb.py --count 50

### 7.3 训练命令

    # T1 抽取模型
    python scripts/finreport-train train-extractor --version v1.0.0

    # T2 embedding
    python scripts/finreport-train train-embedder --version v1.0.0

    # T3 LayoutLM
    python scripts/finreport-train train-layoutlm --version v1.0.0

    # 评估
    python scripts/finreport-train eval-extractor --version v1.0.0

### 7.4 测试命令

    # Java 测试
    cd backend && ./mvnw test
    cd backend && ./mvnw verify -Pintegration  # 含 Testcontainers

    # Python 测试
    cd ai-service && pytest tests/ -v --cov=app

    # 前端测试
    cd frontend && npm run test:e2e

### 7.5 代码质量

    # Java
    ./mvnw checkstyle:check spotbugs:check

    # Python
    ruff check app/ tests/
    black --check app/ tests/

    # 前端
    npm run lint
    npm run type-check

---

## 8. 关键技术约束

### 8.1 GPU 显存（6GB VRAM 硬约束）

- 推理时：单进程只能装 1 个 7B(4bit ~5GB) 或 1 个 1.5B + 1 个 small
- 训练时：必须用 gradient_checkpointing + paged_adamw_8bit + 4-bit 量化
- 多 worker：通过 Redis 分布式锁 fin:lock:model:{modelName} 防冲突
- MQ 消费者：prefetch_count=1

显存预算参考：

| 模型 | 推理 | 训练 |
|---|---|---|
| Qwen2.5-7B 4-bit | ~5 GB | 不训练 |
| Qwen2.5-1.5B 4-bit QLoRA | ~1 GB | ~5.2 GB |
| bge-small-zh LoRA | ~0.2 GB | ~3.8 GB |
| LayoutLMv3 fp16 | ~0.3 GB | ~4.5 GB |

### 8.2 模型调用

- 业务代码禁止直接 import transformers / vllm
- 所有模型调用必须走 ModelHub.generate() 或 ModelHub.embed()
- ModelHub 内部按 scene 路由：EXTRACT / REASON / EMBED / LAYOUT
- 失败降级链：本地 7B → API 72B（DeepSeek/Qwen）

### 8.3 消息队列

- 所有队列 durable=true，消息 delivery_mode=2
- 手动 ack：业务成功才 ack；失败 nack(requeue=false) 进 DLQ
- 消息体必须含 idempotency_key = taskId + step（不含 retry，否则重试时 key 变化导致幂等失效；重试次数通过 headers.x-retry-count 传递）
- traceId 通过消息属性 headers.traceId 透传

### 8.4 数据一致性

- 任务边界 = 事务边界（按 taskId）
- 失败不强制回滚，标记 task.status=FAILED，保留数据供排查
- 同 PDF 重传：基于 pdf_md5 命中 Redis 缓存，跳过解析抽取

### 8.5 安全

- 密码：BCrypt（cost=10）
- JWT：access 1h + refresh 7d，登出加黑名单
- 文件上传：最大 50MB，校验 MIME 类型
- SQL：必须用参数化查询（JPA / R2DBC），禁止字符串拼接
- 日志：脱敏 password / token / pdf_content 字段

---

## 9. AI 协作规范

### 9.1 任务接手流程

1. 先读 CLAUDE.md（本文件）
2. 读对应任务的 spec 章节 和 plan 任务卡
3. 检查依赖任务是否完成（看 docs/progress/m{N}.md）
4. 理解现有代码：用 Grep / Read 浏览相关模块
5. 小步实现：每次只改一个文件，跑测试，提交
6. 更新进度：完成后在 docs/progress/m{N}.md 打勾

### 9.2 禁止事项

- 擅自升级技术栈版本
- 跳过测试直接提交
- 在业务代码直接 import transformers
- 用 print / System.out.println 调试（用 logger）
- 修改 main 分支
- force push
- 删除已有测试用例
- 引入设计文档未提及的新依赖（需先提变更申请）

### 9.3 推荐事项

- 修改前先读相关文件
- 一次只解决一个问题
- 提交前跑 mvnw test / pytest
- 注释解释 为什么 而非 是什么
- 函数超过 50 行考虑拆分
- 文件超过 300 行考虑拆分

### 9.4 代码生成模板

Java Service 方法：

    /**
     * 简短描述。
     *
     * @param param 参数说明
     * @return 返回值说明
     * @throws BusinessException 异常场景
     */
    public Result doSomething(Param param) {
        log.debug([doSomething] 入参={}, param);
        // 业务逻辑
        return result;
    }

Python 异步函数：

    async def do_something(param: Param) -> Result:
        [简短描述]

        Args:
            param: 参数说明

        Returns:
            返回值说明

        Raises:
            AiException: 异常场景
        # 占位
        return result

Vue 组件：

    <script setup lang=ts>
    import { ref, onMounted } from vue
    import type { Report } from @/types

    interface Props {
      reportId: number
    }

    const props = defineProps<Props>()
    const report = ref<Report | null>(null)

    onMounted(async () => {
      report.value = await fetchReport(props.reportId)
    })
    </script>

    <template>
      <div class=report-detail>
        <!-- 模板内容 -->
      </div>
    </template>

---

## 10. 错误处理规范

### 10.1 错误码体系（RFC 9457）

    {
      type: https://finreport.example/errors/{error_code},
      title: {human_readable_title},
      status: 422,
      detail: {specific_detail},
      instance: {request_path},
      traceId: {uuid_for_debugging}
    }

### 10.2 异常分类

| 层 | 基类 | 示例 |
|---|---|---|
| L2 业务异常 | BusinessException | AuthException / ValidationException |
| L2 集成异常 | IntegrationException | AiServiceException / MqException |
| L3 AI 异常 | AiException | ModelLoadException / InferenceTimeoutException |

### 10.3 降级策略

| 场景 | 降级动作 |
|---|---|
| 本地 7B 超时 (>60s) | 切 API 72B |
| 本地 7B OOM | 释放模型 → 重启 ModelHub → 重试 1 次 → 转 API |
| 抽取小模型 JSON 失败 | 重试 1 次 (temp=0.1) → 改用 7B |
| Milvus 不可用 | search_kb 返回 暂不可用，问答继续 |
| MySQL 死锁 | 重试 3 次（指数退避） |

---

## 11. 日志规范

### 11.1 格式（JSON）

    {
      timestamp: 2026-07-13T20:30:00.123Z,
      level: INFO,
      service: backend,
      traceId: a1b2c3d4,
      spanId: e5f6g7h8,
      userId: 1001,
      taskId: f3c2e1a8,
      action: REPORT_UPLOAD,
      message: PDF uploaded,
      extra: {fileSize: 12345678}
    }

### 11.2 日志级别

| 级别 | 使用场景 |
|---|---|
| ERROR | 系统异常、需要立即处理（如 OOM、DB 连接失败） |
| WARN | 业务异常但可降级（如 7B 超时切 API） |
| INFO | 关键业务节点（如任务开始、完成） |
| DEBUG | 调试信息（入参、出参、状态变化） |
| TRACE | 极细粒度（如每个 token 推理） |

### 11.3 traceId 传递

- 前端生成 traceId，写入 HTTP Header X-Trace-Id
- L2 → RabbitMQ 消息属性 headers.traceId
- L2 → L3 HTTP 调用 Header 透传
- 所有日志必须含 traceId

---

## 12. 性能与 SLA

### 12.1 端到端 SLA

| 链路 | 阶段 | 目标 | 超时 |
|---|---|---|---|
| 解析 | PARSE (100页) | < 90s | 180s |
| 解析 | EXTRACT (三表并行) | < 60s | 120s |
| 解析 | CHECK | < 30s | 60s |
| 解析 | REPORT | < 45s | 90s |
| 解析 | 总链路 | < 4min | 8min |
| 问答 | 首 token | < 15s | 30s |
| 问答 | 完整回答 | < 30s | 60s |

### 12.2 性能禁忌

- 在循环里查数据库（用批量查询）
- 在 SSE 流里同步等待 MQ（用背压 buffer）
- 大对象进 Redis（用 hash 分字段）
- PDF 全文进内存（用流式解析）
- 模型推理时切模型（用 model_lock 防抖动）

---

## 13. 文档维护

### 13.1 文档类型

| 类型 | 位置 | 维护时机 |
|---|---|---|
| 设计文档 | docs/superpowers/specs/ | 架构变更时 |
| 实现计划 | docs/superpowers/plans/ | 任务调整时 |
| API 文档 | docs/api/ | 接口变更时 |
| 评估报告 | docs/eval/ | 每次发版 |
| 进度记录 | docs/progress/m{N}.md | 任务完成时 |
| 部署文档 | docs/deployment.md | 部署方式变更时 |

### 13.2 文档与代码一致性

- 接口变更必须同步更新 docs/api/openapi.yaml
- 表结构变更必须同步更新 spec §5.2
- 模型版本变更必须同步更新 model_registry 表 + spec §4.7

---

## 14. Definition of Done

### 14.1 任务级 DoD

1. 代码已提交（Conventional Commits）
2. 单元测试已编写并通过（覆盖率 >= 80%）
3. 集成测试（如适用）通过
4. 文档（API / README 章节）已更新
5. CI 流水线全绿
6. docs/progress/m{N}.md 打勾

### 14.2 里程碑级 DoD

1. 所有任务级 DoD 满足
2. 阶段验收标准全部达成
3. 演示视频片段录制
4. spec 与实现如有偏差，更新 spec

---

## 15. 常见问题速查

### Q1: OOM 怎么办？

A: 检查是否启用 gradient_checkpointing + paged_adamw_8bit + 4-bit 量化；降低 batch_size 到 1；用 gradient_accumulation_steps 补偿。

### Q2: MQ 消息积压？

A: 检查消费者是否卡在模型加载（看 model_lock）；增加 worker（注意显存）；查看 DLQ 是否有死信。

### Q3: SSE 断线重连不工作？

A: 检查客户端是否传 Last-Event-ID；L2 是否从 Redis fin:task:progress:{taskId} 恢复。

### Q4: 抽取 JSON 解析失败？

A: 检查 prompt 是否强约束 JSON；启用 validator 重试（temp=0.1）；仍失败改用 7B 兜底。

### Q5: 显存切换抖动？

A: 检查 EXTRACT 链路是否只用 1.5B、REASON 链路是否只用 7B；调整 model_lock TTL。

---

## 16. 联系与升级

- 本文件由项目维护者管理
- 修改本文件需提 PR + review
- 版本变更记录在文件头 版本 字段

版本：v1.0
最后更新：2026-07-13
