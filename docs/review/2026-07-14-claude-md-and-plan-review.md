# CLAUDE.md 与实现计划审查报告

> **审查日期**：2026-07-14
> **审查范围**：CLAUDE.md（v1.0）、设计文档（v1.0）、实现计划（v1.0）
> **审查人**：AI 协作审查
> **当前状态**：项目骨架已建，代码尚未开始（pre-M1）

---

## 一、总体评价

三份文档（CLAUDE.md / 设计文档 / 实现计划）质量很高，是个人项目中少见的严谨程度。文档之间高度一致，技术选型合理，显存预算精细，SLA 目标务实。

**评分**：

| 文档 | 完整性 | 准确性 | 可操作性 | 一致性 |
|---|---|---|---|---|
| CLAUDE.md | 9/10 | 9/10 | 9/10 | 9/10 |
| 设计文档 | 9.5/10 | 9/10 | 8.5/10 | 9.5/10 |
| 实现计划 | 8.5/10 | 8/10 | 9/10 | 8.5/10 |

---

## 二、CLAUDE.md 逐节审查

### ✅ 优点

1. **§0 项目速览**：5 行说清楚项目是什么，表格一目了然。GPU 约束显式标注，避免 AI 生成不合理的代码。

2. **§1 技术栈版本严格锁定**：版本号精确到 minor，有约束力。但有一个小问题——设计文档中说 PyTorch 2.3.x + CUDA 12.1，CLAUDE.md 一致，没问题。

3. **§3 代码规范**：三层规范覆盖全面。
   - Java record 作为 DTO 的约定很好，符合 Java 17 特性
   - Python 禁止直接 import transformers 是关键约束，在 §8.2 再次强调，双重保护
   - Vue 的 SSE 客户端封装约定避免了组件散落 EventSource 代码

4. **§4 提交规范**：Conventional Commits + scope 按模块划分，粒度控制（单次 < 500 行）合理。

5. **§7 常用命令**：非常实用，AI 可以直接复制执行。开发环境、初始化、训练、测试、代码质量全覆盖。

6. **§8 关键技术约束**：
   - **§8.1 显存预算表**是整份 CLAUDE.md 最有价值的部分之一，精确到 GB
   - **§8.2 ModelHub 路由**确保业务代码不依赖具体模型实现
   - **§8.4 数据一致性**的"不强制回滚"策略适合 Demo 项目
   - **§8.5 安全约束**覆盖了最常见的漏洞

7. **§10 错误处理规范**：RFC 9457 错误码体系专业。降级策略表覆盖了 6GB VRAM 场景下最可能的 5 种失败。

8. **§14 Definition of Done**：任务级和里程碑级 DoD 都是可验证的条件。

9. **§15 常见问题速查**：5 个 FAQ 精准命中 6GB VRAM 开发的典型痛点。

10. **§9 AI 协作规范**：禁止事项和推荐事项都很具体，不是空话。

### ⚠️ 需改进

| # | 位置 | 问题 | 严重度 | 建议 |
|---|---|---|---|---|
| C1 | §1 技术栈 | MinIO 写"最新稳定版"但其他组件都精确到版本号 | 低 | 锁定版本如 `RELEASE.2024-07-xx` |
| C2 | §2.1 模块对应 | 缺少 M11（模型与训练）的 L2 对应——model_registry 表的管理接口应放在哪？ | 中 | 补充：M5 审计与配置下挂 `ModelRegistryController` |
| C3 | §3.1 代码规范 | Java "Service 层方法首行用 logger 记录入参" 可能与 §11 日志规范冲突——DEBUG 级别的入参日志带敏感字段时如何处理？ | 低 | 补充：入参日志需过脱敏 Filter |
| C4 | §6 测试要求 | 只有覆盖率门槛，缺少必测场景列表。设计文档 §7.3.3 有明确场景，CLAUDE.md 应引用 | 低 | 添加交叉引用或直接列出场景 |
| C5 | §8.2 模型调用 | 描述了路由逻辑但未说明如何切换——是通过 ConfigService 还是环境变量？ | 中 | 补充：模型路由通过 `fin.model.{scene}.provider` 配置项控制 |
| C6 | §8.3 消息队列 | `idempotency_key = taskId + step + retry` 逻辑有问题：retry 是可变值，无法去重。应该是 `taskId + step` | **高** | 修改为 `taskId + step`，重试次数存 Redis value |
| C7 | §12 性能 SLA | "问答首 token < 15s" 对于 6GB VRAM + 4-bit 7B 偏乐观，冷启动加载模型可能 > 30s | 中 | 区分冷/热启动 SLA，或说明模型预热策略 |
| C8 | §9.4 代码模板 | Vue 模板缺少 `<style>` 块约定（scoped / CSS Modules / Tailwind？） | 低 | 补充样式约定 |

### 🔴 严重问题：C6 — idempotency_key 定义错误

当前定义：
```
idempotency_key = taskId + step + retry
```

问题：`retry` 是重试次数（0, 1, 2...），同一条消息在不同重试时 retry 不同 → 幂等性失效。

正确做法：
```
idempotency_key = taskId + step  // 固定不变
```
重试次数通过消息属性 `headers.x-retry-count` 传递，或存 Redis。设计文档 §3.8 也提到了 `idempotency_key = taskId + step + retry`，需要同步修正。

---

## 三、实现计划逐阶段审查

### M1（Week 1-2）：基础设施与骨架打通 — 17 任务

**任务拆分质量**：优秀。从"建目录"到"CI 流水线"，粒度均匀，依赖关系清晰。

**问题**：

| # | 任务 | 问题 | 建议 |
|---|---|---|---|
| P1 | M1.02 | 8 个 Dockerfile 一次写完工作量偏大。frontend/backend/ai-service 的 Dockerfile 依赖各自的工程骨架（M1.07/M1.13/M1.15） | 拆为 M1.02a（基础设施容器 mysql/redis/milvus/rabbitmq/minio）+ M1.02b/c/d（应用容器，各自在工程骨架任务之后） |
| P2 | M1.04 | `scripts/init_minio.py` 依赖 MinIO 容器先启动，但 M1.02 的依赖链不包含启动顺序 | 在 M1.02 的 docker-compose 中添加 `depends_on` + `healthcheck` |
| P3 | M1.11 | SSE Last-Event-ID 重连在 M1 做偏早——Redis 进度存储（M1.10 只做了内存路由）还没就位 | M1.11 先做基础 SSE，Last-Event-ID 重连推迟到 M2 或明确在 M1.11 中纳入 Redis 进度存储 |

### M2（Week 3-4）：解析与抽取闭环 — 12 任务

**任务拆分质量**：良好。关键路径清晰。

**问题**：

| # | 任务 | 问题 | 建议 |
|---|---|---|---|
| P4 | M2.04 | "vLLM 或 llama.cpp"模糊。vLLM 在 6GB VRAM + Windows 下支持有限；llama.cpp 更合适但需要 GGUF 格式 | 明确选择 llama.cpp（GGUF），或澄清 Windows 下用 ollama 包装 |
| P5 | M2.12 | 集成测试依赖 3 份真实年报（茅台/平安/宁德），但 M4.01 才爬取数据。这些 PDF 需要提前准备 | 在 M2 开始前单独准备 3 份测试 PDF（可从巨潮手动下载），不依赖 M4 的爬虫 |
| P6 | M2.08 | ExtractDispatcher 用 Redis AtomicInteger 计数 3 条 extract 完成状态——但 Redis 可能重启丢失计数 | 增加 MySQL task_step 表作为 ground truth，Redis 仅做加速缓存 |

### M3（Week 5-6）：勾稽异常与报告生成 — 10 任务

**任务拆分质量**：良好。M3 是核心用户价值的集中体现。

**问题**：

| # | 任务 | 问题 | 建议 |
|---|---|---|---|
| P7 | M3.06 | ECharts 服务端渲染"用 echarts-canvas-js 或 puppeteer"——echarts-canvas-js 在 Python 生态没有原生支持；puppeteer 太重（~300MB + Chromium） | 建议用 pyecharts + snapshot-selenium 或直接用 matplotlib 替代（Demo 阶段图表种类固定） |
| P8 | M3.07 | WeasyPrint 对中文支持需要额外字体配置（SimSun/Noto Sans CJK），否则中文 PDF 乱码 | 任务描述补充：配置中文字体路径 |
| P9 | M3.03 | "同比/环比"异常检测需要历史数据。单份年报没有同比基准 | 明确：第一版只做"科目逻辑异常"（不依赖历史）；同比/环比留到 M5 接入知识库后 |

### M4（Week 7-8）：模型微调 — 15 任务

**任务拆分质量**：细致且务实，是整份计划最扎实的部分。

**问题**：

| # | 任务 | 问题 | 建议 |
|---|---|---|---|
| P10 | M4.02 | 自研 Web 标注页——两周内开发一个标注工具 + 标注 5k 样本，时间偏紧 | 考虑直接用 Label Studio（M4.09 已计划部署），减少前端开发量 |
| P11 | M4.04 | 训练脚本放在 `scripts/` 但训练逻辑在 `ai-service/app/modules/modelhub/trainer/`——跨两个目录，维护时容易漏 | 统一：训练逻辑全放 `ai-service/`，`scripts/` 只放薄 CLI 入口 |
| P12 | M4.01 | 爬取巨潮资讯网——需要注意合规性。巨潮有 robots.txt 限制和反爬 | 增加合规说明：仅用于个人研究，控制请求频率（> 5s 间隔），或使用公开数据集替代 |
| P13 | M4.05 | 评估标准"字段 F1 ≥ 0.85"是乐观估计。原始 Qwen2.5-1.5B 在表格抽取上未经微调的 F1 可能 < 0.5 | 接受：这是微调后的目标，但应补充中间检查点（如 1k 样本时先评估一次决定是否继续） |

### M5（Week 9-10）：Agent 问答与知识库 — 9 任务

**任务拆分质量**：良好。混合 SSE 架构描述清楚。

**问题**：

| # | 任务 | 问题 | 建议 |
|---|---|---|---|
| P14 | M5.03 | "用 outlines/guidance 库约束 LLM 输出 JSON"——outlines 在 Windows 上的支持有限；guidance 与 llama.cpp 集成不稳定 | 先用正则兜底解析 + retry（temperature=0.1）作为 Plan A；structured output 作为 Plan B |
| P15 | M5.07 | 知识库构建依赖 M4.08（微调 embedder），但 M5 整体在 M4 之后。如果 M4 延期，M5.07 会被阻塞 | 在 M5.01-06 期间可以先用原版 bge-small-zh 构建知识库，M4.08 完成后再增量重建（成本低） |
| P16 | M5.09 | "5 个标准问题关键事实命中率 ≥ 0.85"——但标准问题和 ground truth 在哪定义？ | 补充到 `data/benchmark/qa_questions.json` 的设计，建议 10 个问题而非 5 个 |

### M6（Week 11-12）：打磨发布 — 13 任务

**任务拆分质量**：良好。覆盖面全。

**问题**：

| # | 任务 | 问题 | 建议 |
|---|---|---|---|
| P17 | M6.12 | 演示视频脚本放在最后——如果发现重大问题需要回头修，时间不够 | 演示视频在 M3 就可以开始录制片段，M6 只做拼接剪辑 |
| P18 | M6.11 | "干净环境 docker compose up 验证"——没有定义"干净环境"。是 Windows 还是 Linux？谁来验证？ | 明确：在 Windows 11（开发机）和 Ubuntu 22.04 VM 各验证一次 |

---

## 四、跨文档一致性检查

### 设计文档 vs CLAUDE.md

| 检查项 | 设计文档 | CLAUDE.md | 一致？ |
|---|---|---|---|
| 技术栈版本 | PyTorch 2.3.x | PyTorch 2.3.x + CUDA 12.1 | ✅ |
| 架构分层 | L1-L5 | L1-L5 | ✅ |
| 模块编号 | M1-M11 | M1-M11 | ✅ |
| 模型选择 | Qwen2.5-7B/1.5B/bge/LayoutLMv3 | 同上 | ✅ |
| SLA 目标 | < 4min 总链路 | < 4min 总链路 | ✅ |
| 错误码 | RFC 9457 | RFC 9457 | ✅ |
| 测试覆盖率 | ≥ 80% 核心模块 | ≥ 80% 核心模块 | ✅ |
| 分支策略 | main/develop/feature/fix/release | main/develop/feature/fix/release | ✅ |
| ModelHub 路由 | EXTRACT/REASON/EMBED/LAYOUT | 同上 | ✅ |

### 设计文档 vs 实现计划

| 检查项 | 设计文档 | 实现计划 | 一致？ |
|---|---|---|---|
| 6 里程碑 | M1-M6 | M1-M6 | ✅ |
| 76 任务 | 提及 | 明确 76 任务 | ✅ |
| MQ 拓扑 | 4 exchange + 6 queue | M1.06 声明 | ✅ |
| 12 张表 | 完整 DDL | 5 个 Flyway 迁移 V1-V5 | ✅ |
| 25 个 API | 清单列出 | 分散在各 M1/M2/M5 任务 | ✅ |
| 3 个训练任务 | T1/T2/T3 | M4.04-M4.11 | ✅ |
| 显存预算 | 详细预算表 | 在任务中引用规范 | ✅ |

### 发现的不一致

| # | 问题 | 设计文档 | 实现计划/CLAUDE.md | 建议 |
|---|---|---|---|---|
| D1 | M9 工具数量 | spec §2.3 M9 列出 6 个工具 | plan M5.02 也是 6 个，但 CLAUDE.md §6.4 测试列表里提到"模型降级（mock 7B 超时 → 切 API）"——这个降级逻辑也需要一个内部工具？ | 无需工具。降级是 ModelHub 内部行为，不暴露为 Agent 工具 |
| D2 | 缓存 TTL | spec §5.4: `fin:cache:extract:{pdfMd5}:{step}` TTL 7d | CLAUDE.md §8.4: "pdf_md5 命中 Redis 缓存"——未指定 TTL | 统一为 7d |
| D3 | Milvus 端口 | 设计文档未指定 | 计划未指定。Milvus 2.4 默认 19530 | 在 docker-compose 中明确 |
| D4 | 后端 R2DBC vs JPA | 设计文档 §5.2.2 用了 JPA 风格的 `@Entity`/Repository，但 §1.2 写 WebFlux 应配 R2DBC | CLAUDE.md §8.5 写"JPA / R2DBC"，两者混用 | WebFlux 项目建议统一用 R2DBC，普通查询不需要 JPA 的 full ORM |

---

## 五、关键风险再评估

基于对三份文档的综合审查，补充设计文档 §7.5 风险表：

| 风险 | 原评估 | 修正评估 | 理由 |
|---|---|---|---|
| 6GB VRAM OOM | 概率 高 | 保持 | 设计文档的显存预算是理论值，实际 PyTorch 缓存 + CUDA context > 显存预算 10-15% |
| 小模型效果差 | 概率 中 | **升级为 高** | Qwen2.5-1.5B 在中文财务表格上未经大量金融数据 SFT，F1 目标 0.85 需要 > 5k 高质量样本支撑 |
| **新增：Windows 兼容** | — | 概率 高 | llama.cpp/outlines/WeasyPrint/PP-Structure 在 Windows 上的兼容性不如 Linux。建议核心开发在 WSL2 或 Linux VM |
| **新增：单机 8 service** | — | 概率 中 | 8 个 Docker 容器 + 模型推理在笔记本 16GB RAM 下可能内存不足。建议开发时按需启动 |
| PDF 解析失败 | 概率 中 | 保持 | A 股年报格式多样（扫描件/文本 PDF/混合），30% 样本可能需要 OCR 兜底 |
| **新增：1.5B + 7B 并行** | — | 概率 低 | 设计文档 §3.9 明确说明两者不共存，但 plan 三表并行（M2.08）用的是 7B 通用 prompt（M2.06），不存在并行冲突。M4.15 替换为 1.5B 后也不冲突 |

---

## 六、进度与实际状态

### 6.1 当前实现状态

```
目录结构        ✅ 已建立（与 plan §1 一致）
CLAUDE.md       ✅ 已编写（v1.0）
设计文档         ✅ 已编写（v1.0）
实现计划         ✅ 已编写（v1.0）
代码             ❌ 全部为空（backend/ai-service/frontend/deploy/scripts 无实质文件）
进度记录         ❌ docs/progress/ 目录未创建
Git             ❌ 未初始化
```

**当前阶段**：pre-M1，准备开工。

### 6.2 建议的开工前检查清单

- [ ] `git init` + `.gitignore`（M1.01）
- [ ] 在 Windows 上验证 WSL2 或 Docker Desktop 可用
- [ ] 下载 3 份测试年报 PDF（为 M2 测试准备）
- [ ] 下载 Qwen2.5-7B-Instruct GGUF 4-bit（验证 llama.cpp 可用）
- [ ] `nvidia-smi` 确认 CUDA 12.1 已安装
- [ ] 创建 `docs/progress/` 目录
- [ ] 修正 C6（idempotency_key 定义）

---

## 七、改进建议汇总

### 立即修复（阻塞性问题）

| ID | 优先级 | 描述 |
|---|---|---|
| C6 | 🔴 高 | 修正 CLAUDE.md §8.3 和设计文档 §3.8 的 `idempotency_key` 定义 |
| P5 | 🔴 高 | 在 M2 前准备测试 PDF（不依赖 M4 爬虫） |
| D4 | 🟡 中 | 明确 L2 数据访问层是用 R2DBC 还是 JPA，全文统一 |

### 建议修改（质量改进）

| ID | 优先级 | 描述 |
|---|---|---|
| P4 | 🟡 中 | 明确 Windows 下的 LLM 推理后端选择 |
| P7 | 🟡 中 | 确认 ECharts 服务端渲染方案在 Python 环境的可行性 |
| P9 | 🟡 中 | M3 异常检测先做科目逻辑异常，同比/环比推迟 |
| P15 | 🟡 中 | M5 知识库构建提前用原版 embedder 做初版 |
| C2 | 🟢 低 | 补充 ModelRegistry 的 L2 归属 |
| C7 | 🟢 低 | 区分模型冷/热启动 SLA |

### 结构优化

1. **训练脚本归属**：统一放 `ai-service/` 下，`scripts/` 只做薄入口
2. **标注工具复用**：M4.02 考虑直接用 Label Studio，减少自研标注页
3. **增量演示**：建议在 M3 完成后就录制第一版演示视频片段，M6 做最终剪辑

---

## 八、总结

三份文档的整体质量**远超个人 Demo 项目的平均水平**。架构设计有完整的理论支撑（ReAct / QLoRA / MQ 拓扑 / SSE 混合模式），技术约束务实（6GB VRAM 硬约束贯穿全篇），任务拆分粒度合理（76 任务 / 12 周）。

**核心优势**：
- 显存预算精确到小数点，可落地
- 降级策略覆盖了最常见的 5 种失败场景
- MQ 拓扑 + SSE 混合模式解决了"可靠控制面 + 低延迟数据面"的矛盾
- 训练设计有完整的评估闭环

**需要关注**：
- 0 行代码已写，12 周时间表需要严格执行
- Windows 兼容性是隐藏风险，建议 WSL2
- 1.5B 小模型 F1 ≥ 0.85 目标偏高，需要充足的高质量标注数据
- M4 和 M5 有串行依赖，M4 延期会传导到 M5

**建议开工顺序**：
1. 修复 C6（idempotency_key）
2. Git init + 完成 M1.01
3. 验证 Docker Desktop + CUDA + llama.cpp 可用
4. 按 M1 → M2 → ... 顺序推进

---

*审查完成于 2026-07-14。建议在 M1 完成后做一次中期对照检查。*
