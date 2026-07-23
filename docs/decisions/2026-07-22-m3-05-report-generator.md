# 2026-07-22 M3.05 ReportGenerator NLG

## 背景

M3.01–M3.04 已完成 L3 Reasoner 三层能力（规则引擎 / LLM 复核 / 异常检测）
+ L2 勾稽结果落库。Reasoner → Report 链路需要 L3 M10 ReportGenerator
将 `FinancialStatement` + `CheckResult` + 可选检索段落喂给 7B 模型，
产出 5 段式 Markdown 报告（公司概况 / 财务概览 / 三表分析 / 异常与风险 / 结论）。

plan M3.05 验收标准：

* 5 段固定结构齐全（公司概况 / 财务概览 / 三表分析 / 异常与风险 / 结论）
* 引用真实数据（数值、科目名、规则结果、异常描述）
* 失败降级到模板，5 段齐全但内容较朴素

实现完成后做了全面代码审查，发现 3 个 Major（无 Blocker / Critical），
全部修复后通过 L3 136 个 M3 相关测试（M3.01 + M3.02 + M3.03 + M3.05），
4 个目标模块覆盖率全部 100%。

## 决策列表

1. **5 段固定结构（spec §2.3 M10）** — `ReportSectionType` 枚举固定 5 段
   且提供 `chinese_name` 属性。`_parse_sections` 严格校验 LLM 输出
   `sections` 长度 = 5 + 每段 title/content 非空；顺序错乱时按位置归一
   为 5 段固定 type（容错策略，保留 LLM 给的具体 title）。LLM 输出
   段数 ≠ 5 直接降级到模板，避免前端 Tab 渲染异常。

2. **异步 + `asyncio.to_thread` 包装** — 与 `LLMReviewer` 一致，
   `ModelHub.generate` 是 torch 阻塞调用；用 `asyncio.to_thread` 避免阻塞
   事件循环，不引入并发（spec §8.1 单进程只能装 1 个 7B，并发无收益
   反而争抢 GPU）。`asyncio.TimeoutError` 与 `Exception` 分支捕获，所有
   异常都走降级路径不外抛。

3. **不可变输入** — 不修改 `FinancialStatement` / `CheckResult`；产出
   新的 `ReportResult`。测试 `test_should_not_mutate_statement` /
   `test_should_not_mutate_check_result` 验证；对齐 `LLMReviewer` /
   `AnomalyDetector` 的不可变风格，便于 L2 落库前后对比。

4. **失败降级链（spec §10.3）** — LLM 超时 / 异常 / JSON 解析失败 /
   sections 不合规 都不抛 `Exception`，走模板降级产出 5 段齐全的
   `ReportResult`（`fallback=True`），保证 Reasoner → Report 链路不被
   LLM 失败阻断。`ReportResult.fallback` 字段便于 M3.09 前端展示降级
   提示横幅（"本报告由模板生成，建议人工复核"）。

5. **模板降级保留 token metrics** — 失败路径仍透传 `prompt_tokens` /
   `completion_tokens` / `latency_ms` / `raw_text`，便于 M3.10 SLA
   度量与监控告警。即使 LLM 输出无法解析为 JSON，原始输出也保留在
   `raw_text` 中便于事后排查 prompt 与输出契约偏差。

6. **三层 JSON 解析降级** — 对齐 `Extractor._extract_json_object` /
   `LLMReviewer._extract_json_object`：

   1. 直接 `json.loads` 去空白后的整段文本
   2. 解包 ```json ... ``` 围栏后重试
   3. 贪婪匹配首个 `{...}` 子串后重试

   Qwen2.5 偶尔会把 JSON 包在 ```json``` 围栏里输出，三层降级提升
   解析鲁棒性。

7. **三表上下文截断** — 每表最多 15 个科目进 prompt（`_MAX_STATEMENT_ITEMS_PER_TABLE`），
   避免 prompt 过长撑爆 7B 上下文（spec §3.7 REPORT 45s SLA）。
   KB 检索段落每条 ≤ 500 字符、最多 5 条（`_MAX_KB_SNIPPETS`）。

8. **KB 检索段落可选** — 当前 M3 阶段知识库未建好（M5），
   `kb_snippets` 支持 `None` 或空列表；M5 落地后由调用方填充。
   `_format_snippets` 在空时返回 `(无检索段落)` 占位，prompt 模板
   不变。

9. **温度 0.3 / max_tokens 2048 / timeout 45s** — 报告需自然语言，
   温度略高避免死板；但仍偏低避免幻觉（spec §10.3 复核用 0.1，报告
   用 0.3）。5 段报告较长，需要较大 token 预算（M3.02 复核 512 /
   M3.05 报告 2048）。timeout 45s 对齐 spec §3.7 REPORT 链路 SLA
   目标（不是超时上限 90s）；超时上限由 L2 状态机层面的 retry 计数器
   处理。

10. **`to_markdown` 渲染** — `ReportResult.to_markdown()` 按 5 段顺序
    拼成完整 Markdown，每段以 `## {title}` 作为 H2 标题，段间空行分隔。
    M3.07 WeasyPrint 直接消费此 Markdown 输出转 PDF。

11. **`_STATEMENT_LABELS` 改用 `StatementType.chinese_name`（M2/M3 修复）** —
    原实现 `_STATEMENT_LABELS` 字典硬编码字符串（"资产负债表" / "利润表"
    / "现金流量表"），与 `StatementType.chinese_name` 冗余，是双重事实
    源（spec §13.2 文档与代码一致性）。改为
    `StatementType.BALANCE_SHEET.chinese_name` 等属性引用，单点维护。

12. **Decimal 精度** — 数值字段统一 `Decimal(str(float))` 避免 IEEE754
    精度损失（spec §8.4 数据一致性）。与 `accounting_rules.py` /
    `anomaly_detector.py` / `llm_reviewer.py` 风格一致。

13. **不引入新依赖** — 复用现有 `ModelHub.generate` + `asyncio.to_thread`
    + `re` + `json`，不引入 `pytest-asyncio`（测试用 `asyncio.run()` 包装，
    对齐 `test_m3_llm_reviewer.py` 风格）。

14. **`success` / `all_sections_present` 属性** — `success = not fallback
    and error is None`；`all_sections_present` 验证 5 段齐全且 content
    非空。便于 M3.08 L2 写 `report_artifact` 时快速判断报告是否合格，
    不合格可触发重试或人工介入。

## 已完成 checklist

* 新增 `app/schemas/report.py`（ReportSectionType / ReportSection /
  ReportResult + `to_markdown` / `success` / `all_sections_present`）
* 新增 `app/modules/generator/prompts.py`（`build_report_prompt` +
  `_format_rules` / `_format_anomalies` / `_format_snippets` /
  `_fmt_decimal`）
* 新增 `app/modules/generator/report_generator.py`（`ReportGenerator`
  + `_extract_json_object` / `_parse_sections` / `_build_fallback_report`）
* 修改 `app/modules/generator/__init__.py` 导出 `ReportGenerator` +
  `build_report_prompt`
* 新增 50 个测试用例（8 个测试类：TestBuildReportPrompt /
  TestGenerateSuccess / TestGenerateFallback / TestImmutability /
  TestFallbackContent / TestReportResultHelpers / TestDefaultsAndEdgeCases
  + 围栏解包专项测试）
* 修复 M1（死代码 TYPE_CHECKING）/ M2 + M3（_STATEMENT_LABELS 双重事实源）
* 更新 `docs/progress/m3.md` M3.05 交付说明
* M3.01 + M3.02 + M3.03 + M3.05 共 136 个 L3 测试全部通过
* 4 个目标模块覆盖率全部 100%（prompts.py / report_generator.py /
  schemas/report.py / __init__.py）
* ruff check / ruff format（与 black 兼容）全部通过

## 发现的风险

1. **LLM 输出段数 ≠ 5 直接降级** — 当前实现严格校验 sections 长度 = 5，
   不容错 4 段或 6 段。若 7B 模型实际生成时偶尔合并/拆分段落，会
   频繁触发模板降级。M3.10 端到端测试需观察真实模型的段数稳定性，
   必要时放宽到「至少包含 5 段固定 type 即可，多余段忽略」策略
   （需要改 `_parse_sections` 实现按 title 模糊匹配 type 而非按位置归一）。

2. **prompt 三表上下文截断** — 每表前 15 个科目进 prompt。若关键科目
   （如「资产总计」「净利润」）不在前 15 个位置，LLM 可能漏引用。
   M3.10 端到端测试需观察 LLM 实际引用的科目覆盖度，必要时改为
   「按重要性筛选」而非「按顺序前 N 个」（需要 M3.07 前增加科目
   重要性排序逻辑）。

3. **`fallback=True` 时的 raw_text 可能很长** — 失败路径保留原始
   LLM 输出到 `raw_text` 字段。若 LLM 输出 2048 tokens 但无法解析，
   `raw_text` 会占用较多存储。M3.08 L2 写 `report_artifact` 时需
   考虑是否截断或单独存到 MinIO 而非 MySQL。

4. **温度 0.3 仍可能幻觉** — 当前 prompt 已强约束「禁止编造」+「必须
   引用真实数据」，但 7B 模型仍可能在数值上幻觉（如把 1300 亿写成
   1300 万）。M3.10 端到端测试需人工抽检 LLM 生成的数值与抽取结果
   是否一致；若幻觉率高，需在 `_parse_sections` 后增加数值校验层
   （提取 content 中的数字与 `statement` 对比）。

5. **KB 检索段落未实际接入** — `kb_snippets` 参数当前由调用方填充，
   M3 阶段调用方传 None。M5 知识库落地后需要由 L2 编排层在 REPORT
   阶段先调用 L3 `search_kb` 接口拿到段落再传给 `ReportGenerator`。
   这条链路在 M3.05 没有端到端测试覆盖。

## 下一步行动项

* M3.06 L3 M10 ECharts 服务端渲染 — 生成图表 PNG 嵌入报告
* M3.07 L3 M10 Markdown → PDF — WeasyPrint 消费 `to_markdown()` 输出
* M3.08 L2 写 report_artifact + 上传 MinIO — 持久化报告 Markdown + PDF
* M3.09 前端勾稽页 + 异常页 + 报告页 — 消费 `ReportResult` 渲染 Tab
* M3.10 端到端 SLA 测试 — 真实 7B 模型 + 真实茅台年报验证
  `ReportGenerator` 在真实 GPU 环境下的延迟（目标 < 45s）与生成质量
