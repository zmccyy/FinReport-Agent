# 2026-07-22 Skills 保留策略与项目文档同步

## 背景

当前 Codex 环境包含约 134 个 skills，覆盖 Web、文档、安全、生命科学、NGS、数据分析和其他领域。FinReport Agent 的实际技术栈是 Vue3 + Element Plus、SpringBoot WebFlux、FastAPI、MySQL、Redis、Milvus、RabbitMQ、MinIO 和 ModelHub。大量通用或领域 skills 不应影响本项目的默认实现。

截至 2026-07-22，仓库实际状态为：

- M1 基础设施与骨架已完成；
- M2 解析与抽取闭环已完成；
- M3.01 勾稽规则引擎已完成，M3.02–M3.10 尚未完成；
- 当前 HEAD 为 `8506b38`，包含 M3.01 规则引擎、schemas 和 20 个测试。

## 决策列表

1. 不删除用户全局 skills，改为通过 `docs/skills.md` 建立项目级默认白名单、条件启用规则和冲突约束。
2. 默认核心 skills 固定为：`api-design`、`security-best-practices`、`pdf`、`frontend-design`、`webapp-testing`、`systematic-debugging`、`verification-before-completion`、`requesting-code-review`。
3. React、shadcn、Stripe、Supabase/PostgreSQL、Hyperframes、Mixpanel、生命科学、NGS 等 skills 默认排除，避免覆盖 Vue3、MySQL 和 ModelHub 架构。
4. 正式 Codex Security skills 仅在用户明确要求安全扫描、漏洞复核、威胁建模或加固时启用。
5. 未经用户明确授权，不启动并行子代理、不创建额外 git worktree。
6. `AGENTS.md` 和 `CLAUDE.md` 统一引用同一份 skills 策略；README 和实现计划同步到当前 M3.01 状态。
7. 任务实时状态以 `docs/progress/m{N}.md` 和 `docs/decisions/` 为准，不能继续使用实现计划中“ M2 待开始”的过期状态描述。

## 已完成 checklist

- [x] 新增 `docs/skills.md`。
- [x] 在 `AGENTS.md` 中加入 skills 使用规范和文档链接。
- [x] 将 `CLAUDE.md` 与 `AGENTS.md` 的项目规范同步。
- [x] README 更新为 M1/M2 已完成、M3.01 已完成的当前状态。
- [x] 实现计划更新为 M3 进行中，并声明实时进度来源。
- [x] 记录 PyTorch 依赖范围与项目锁定版本之间的待处理差异。

## 发现的风险

- `ai-service/pyproject.toml` 当前声明 `torch>=2.3,<2.6`，而项目规范目标是 PyTorch 2.3.x；GPU 部署前需要单独收紧版本并补充验证。
- `transformers` 出现在生产依赖中，但只能由 ModelHub 内部使用；业务模块不得直接 import。
- 全局 skills 仍然存在，项目策略只能约束本仓库内的激活方式，不能改变其他仓库的全局行为。

## 下一步行动项

1. M3.02 实现 LLM 复核时，先补充 ModelHub 适配边界和失败降级测试。
2. 在 GPU 环境准备阶段，将 PyTorch 版本范围收紧到 2.3.x，并更新 spec、lock/依赖文件和部署验证记录。
3. 后续每次里程碑完成时，同步更新 `README.md`、实现计划、progress 和 decision 记录。
