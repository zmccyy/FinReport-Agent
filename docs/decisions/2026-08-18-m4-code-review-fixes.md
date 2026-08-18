# M4 全量代码审查 + 修复批次（code-review 双轴）

> 日期：2026-08-18 | 状态：已完成
> 审查范围：`git diff a6d77db...HEAD`（M4.02 → M4.09 全部 15 个提交，70 文件）
> 方法：Standards 轴（AGENTS.md 规范 + Fowler 坏味道基线）× Spec 轴（决策记录 + progress/m4.md + spec §3.5/§4.x）

## 审查结论（通过项）

- **API 重试语义**（spec §8.1/§4.2）：429/5xx/网络错误退避 1s/2s 重试 2 次、4xx 不重试、超时抛 `InferenceTimeoutException` 不重试——`api_backend.py` 实现与规格逐条一致；`LLM_API_KEY` 不出现在任何日志（仅记 base_url/model）
- **MQ 幂等键**（AGENTS.md §8.3）：L3 `taskId:STEP_NAME`（大写）与 L2 `TaskMessageProducer` 契约（`task-retry:PARSE` 测试断言）对齐；不含 retry 计数，重试计数走 header
- **降级链**（spec §10.3）：LLMReviewer 保留规则结果 + 追加降级标记；ReportGenerator 降级模板报告（5 段齐全）——两条链均不抛异常阻断
- **json_mode 协议**：extractor/validator/reviewer/generator prompt 均含 "JSON" 字样（DeepSeek json_object 协议要求）
- **M4.08 清理彻底性**：Scene.LAYOUT 已删；8 项本地模型配置已清（`model_embed_path` 为合法保留）；transformers 系依赖仅 `BgeSmallEmbedder` 一处入口（AGENTS.md §8.2 例外条款）
- **数据通路**（决策 #6/§4.4）：parse 产物落 MinIO `parsed/{taskId}.json`；extract 从 MinIO 拉取；check/report 只读 MySQL；report 已路由 generator handler（决策 #7）

## 发现问题与修复（3 处，均已修复）

| # | 问题 | 轴 | 定性 | 修复 |
|---|---|---|---|---|
| 1 | `config.py` 死配置 `model_load_timeout_seconds=300`：M4.08 移除 GPU 栈（唯一消费方）后全仓库零引用残留 | Standards（死代码） | 硬伤 | 删除字段，留注释说明去向 |
| 2 | `schemas/parse.py` `MockDocument` 类名：`/parse/upload` 真实解析数据顶着 "Mock" 名字（Mysterious Name）；前端/L2/E2E 均不依赖该端点，仅单测 | Standards（坏味道） | 判断性 | 改名 `DocumentSummary`，wire 契约（字段形状/默认值）零变化 |
| 3 | `mq/consumer.py` 模块 docstring 仍写 "M1 mock processing chain"——M4 后链路已全真实 | Standards（过期文档） | 硬伤 | 重写为真实路由说明 |

## 未完成部分（M4.10 范围，非缺陷）

- `model_registry` 注册 deepseek-chat（extract/reason）+ bge-small-zh-v1.5（embed）
- 真实 PDF 全链路验证 + `eval_m2_f1` 复测（F1 ≥ 0.85）
- prod 镜像 < 3GB 实测（本机 Debian CDN 502 阻断构建，待网络恢复）
- embed() 真实模型 512 维输出验证（bge 权重下载 + docker cp 进卷）

## 回归验证

- 517 passed / 1 skipped（修复前后一致）
- ruff check + format 全过（consumer.py 行尾混用已规范化）

## 下一步行动项

1. M4.10 注册与端到端（上表四项）
2. prod 镜像构建复测（CDN 恢复后）
