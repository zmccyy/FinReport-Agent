# FinReport Agent — Skills 使用策略

> 版本：v1.0  
> 最后更新：2026-07-22  
> 适用范围：本仓库内的 Codex、Claude Code 及其他 AI 协作工具

## 1. 目的与边界

本仓库当前运行环境包含大量通用及领域插件 skills。它们是**按需激活的工具箱**，不是本项目的技术规范，也不应自动覆盖仓库内的 `AGENTS.md`、设计文档或实现计划。

本文件只定义项目级使用策略：

- 不删除用户全局安装的 skills；无关 skills 默认不激活。
- 任务相关 skill 可以提供方法和检查清单，但不能改变项目锁定的技术栈、目录结构、模型调用边界或测试门槛。
- 当 skill 与系统指令、开发者指令、用户要求或本仓库文档冲突时，按以下顺序处理：系统指令 > 开发者指令 > 用户要求与 `AGENTS.md` > 设计/实现文档 > skill 建议。
- 任务完成状态以 `docs/progress/m{N}.md` 和最近的决策记录为准；实现计划用于描述目标、依赖和验收方式。

## 2. 默认核心 skills

以下 skills 可以作为本项目的默认工作集：

| Skill | 适用范围 | 使用约束 |
|---|---|---|
| `api-design` | SpringBoot、FastAPI、SSE、内部服务 API | 遵循 `docs/api/openapi.yaml` 和 RFC 9457 错误结构 |
| `security-best-practices` | 日常安全检查与实现建议 | 必须遵循 JWT、上传、SQL、日志脱敏和限流规范 |
| `pdf` | 财报 PDF 解析、OCR、表格和报告 PDF | 使用流式/分阶段处理，不将全文一次性载入内存 |
| `frontend-design` | Vue3 页面、组件和交互设计 | 输出必须兼容 Vue 3.4、TypeScript、Element Plus 和 Pinia |
| `webapp-testing` | 前端本地验证和 Playwright E2E | 以现有 Vite/Vue 工程为测试目标，不改用 React 测试栈 |
| `systematic-debugging` | 失败测试、MQ、SSE、容器和模型问题 | 先复现、定位边界，再修改代码 |
| `verification-before-completion` | 声称完成、修复或通过前的验证 | 执行与任务范围匹配的测试、lint、类型检查和文档检查 |
| `requesting-code-review` | 提交前审查准备 | 遵循一个任务一个 PR、Conventional Commits 和禁止 force push |

## 3. 条件启用 skills

### 3.1 计划和测试

```text
superpowers:writing-plans
superpowers:executing-plans
superpowers:test-driven-development
```

- 已存在设计文档和实现计划时，优先使用现有文档，不重复创建另一套计划。
- TDD 推荐用于核心 Java/Python 业务模块；配置、文档、一次性脚本和纯样式调整不强制执行“先写失败测试”。
- 任何 skill 都不能绕过项目规定的测试和覆盖率门槛。

### 3.2 正式安全扫描

```text
codex-security:security-scan
codex-security:security-diff-scan
codex-security:deep-security-scan
codex-security:threat-model
codex-security:attack-path-analysis
codex-security:finding-discovery
codex-security:validation
codex-security:triage-finding
codex-security:fix-finding
codex-security:vulnerability-writeup
codex-security:track-findings
codex-security:propose-security-hardening
```

只在用户明确要求安全扫描、漏洞复核、威胁建模或安全加固时启用。普通代码修改只使用 `security-best-practices`。

### 3.3 报告和文档格式

```text
xlsx
docx
pptx
```

只有当 Excel、Word 或演示文稿是任务的主要输入/输出时启用。财报 PDF 解析仍以 `pdf` 为主。

### 3.4 其他条件任务

```text
doc-coauthoring
mcp-builder
find-skills
skill-creator
internal-comms
```

这些 skills 不参与普通业务实现，仅在对应的文档协作、MCP 构建、skill 管理或内部沟通任务中启用。

## 4. 禁止或默认排除的方向

以下 skills 不得影响 FinReport Agent 的默认实现：

```text
build-web-apps:react-best-practices
build-web-apps:shadcn
build-web-apps:stripe-best-practices
build-web-apps:supabase-postgres-best-practices
hyperframes:*
mixpanel-headless:*
life-science-research:*
ngs-analysis:*
zotero
slack-gif-creator
algorithmic-art
canvas-design
brand-guidelines
theme-factory
web-artifacts-builder
```

原因：它们分别偏向 React/shadcn、Stripe、Supabase/PostgreSQL、网页艺术产物、产品分析、生命科学、NGS 或文献管理，与当前项目的 Vue3 + Element Plus + MySQL + FastAPI + ModelHub 架构无关或存在默认技术路线冲突。

## 5. 项目硬约束

### 5.1 前端

- 只使用 Vue 3.4 Composition API、`<script setup lang="ts">`、Element Plus 和 Pinia。
- API 调用统一放在 `frontend/src/api/`；SSE 客户端统一放在 `frontend/src/api/sse.ts`。
- 不引入 React、Next.js、shadcn/ui 或 Supabase 作为替代架构。

### 5.2 AI 服务

- 业务模块不得直接 import `transformers` 或 `vllm`。
- 模型调用统一经 `ModelHub.generate()` 或 `ModelHub.embed()`。
- 遵循 RTX 4050 Mobile 6GB VRAM、4-bit 量化、QLoRA 和 Redis model lock 约束。
- 外部模型 API 只能通过 ModelHub 的适配层接入，不能从 Controller、Extractor、Reasoner 或 Agent 直接调用。

### 5.3 数据和基础设施

- 主数据库为 MySQL 8.0，不使用 Supabase/PostgreSQL 方案替代。
- 消息队列为 RabbitMQ 3.13，缓存和分布式锁为 Redis 7.2，向量库为 Milvus 2.4，对象存储为 MinIO。
- 不能因为 skill 的默认示例而引入 Stripe、Supabase、PostgreSQL 或其他未在 spec 中批准的新依赖。

### 5.4 协作流程

- 未经用户明确授权，不启动并行子代理，不使用 `subagent-driven-development`。
- 未经用户明确要求，不创建额外 git worktree；默认在当前仓库分支协作。
- 修改应保持小步、可验证，并同步更新对应的 progress、API 或决策文档。
- 不允许 skill 覆盖项目的版本锁定、分支策略、日志规范、错误处理和 Definition of Done。

## 6. 当前仓库状态（2026-07-22）

| 里程碑 | 当前状态 | 依据 |
|---|---|---|
| M1 | 基础设施与骨架已完成 | `docs/progress/m1.md` |
| M2 | 解析与抽取闭环已完成 | `docs/progress/m2.md` |
| M3 | 进行中，M3.01 已完成 | `docs/progress/m3.md`、`8506b38` |
| M4 | 未开始 | `docs/progress/m4.md` |
| M5 | 未开始 | `docs/progress/m5.md` |
| M6 | 未开始 | `docs/progress/m6.md` |

当前工作重点是 M3.02–M3.10：LLM 复核、异常检测、结果落库、报告生成、报告 PDF、前端页面和端到端 SLA。

## 7. 发现的文档/配置风险

- 目标规范锁定 PyTorch 2.3.x，但 `ai-service/pyproject.toml` 当前声明为 `torch>=2.3,<2.6`；在 GPU 部署前需要单独提交版本收紧变更。
- `transformers` 可以作为 ModelHub 的内部依赖，但业务代码边界必须继续由本文件和 `AGENTS.md` 约束。
- `AGENTS.md`、`CLAUDE.md`、README 和实现计划的状态描述需要在里程碑变更时同步更新。


