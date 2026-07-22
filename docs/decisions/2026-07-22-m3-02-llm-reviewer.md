# 2026-07-22 M3.02 LLM 复核勾稽实现

## 背景

M3.01 已完成 3 条硬编码勾稽规则引擎（`BalanceSheetIdentityRule` /
`NetIncomeToRetainedEarningsRule` / `CashFlowVsNetIncomeRule`），但失败规则
只能给出固定提示（如"资产负债恒等式不成立，需排查科目分类错误"），无法
解释具体差异原因。

spec §2.3 M8 要求"硬编码 + LLM 复核"双层：硬编码规则失败时调用 7B 复核，
让 LLM 根据 A 股年报披露惯例解释差异来源（如科目分类口径不同、其他综合
收益漏列、归母/少数股东权益拆分差异等）。plan M3.02 验收标准：LLM 能解释
差异原因；note 字段填充；构造故意不平衡的财报测试。

## 决策列表

1. **触发条件分级** — 仅复核 `severity ∈ {WARN, ERROR}` 的规则；`INFO` 已通过
   无需复核；`CRITICAL` 是科目缺失/规则异常，LLM 无数据可分析。降级路径不调
   LLM 节省显存。
2. **与 RuleEngine 解耦** — `RuleEngine` 保持纯同步、无 IO（spec §3.2 L3 算法
   分层）；`LLMReviewer` 是独立异步类，调用方（M3.04 L2 编排）按需
   `await reviewer.review(...)`。未调 LLM 复核的链路零开销，单元测试也无需
   mock LLM。
3. **同步 generate 包到 to_thread** — `ModelHub.generate` 是 torch 阻塞调用；
   用 `asyncio.to_thread` 避免阻塞事件循环。不引入 `asyncio.gather` 并发
   （spec §8.1 单进程只能装 1 个 7B，并发无收益反而争抢显存）。
4. **不可变模型** — `RuleResult` / `CheckResult` 是 Pydantic 不可变模型；复核
   产出新对象（`model_copy(update=...)`），原对象保持不变便于 L2 落表前后对比
   和审计追溯。
5. **失败降级链（spec §10.3）** — LLM 超时 / JSON 解析失败 / 异常 / 空 reason
   都不抛 `Exception`，保留原 note 并追加标记：
   - `[LLM 复核失败: {error}]` — generate 抛异常
   - `[LLM 输出无法解析为 JSON]` — 输出非 JSON
   - `[LLM 复核超时]` — `asyncio.to_thread` 超时
   - `[LLM 返回空 reason]` — reason 字段为空字符串
   
   保证 Reasoner 链路不被 LLM 失败阻断，单条规则降级不影响其他规则。
6. **prompt 强约束 JSON** — 对齐 `extractor.prompts` 风格，输出
   `{"reason": "...", "is_explained": true/false}`；`is_explained=false` 时
   note 追加"建议人工排查"后缀，提示 LLM 也无法定位差异。
7. **三表上下文截断** — prompt 中每张表最多 15 个科目（A 股年报主表典型
   30-50 个科目），避免 prompt 过长撑爆 7B 上下文（spec §3.7 REASON 链路
   60s SLA）。
8. **confidence 不变** — LLM 复核只解释差异，不改通过/失败状态；
   `CheckResult.confidence` 复核前后一致。后续 M3.03 异常检测会独立调整
   confidence。
9. **`RuleResult.llm_reviewed` 字段** — 新增布尔标记，默认 `False`。L2
   `CheckWriter`（M3.04）落表时区分"硬编码规则提示" vs "LLM 复核解释"，
   便于前端勾稽页展示复核状态徽章。
10. **不引入 `pytest-asyncio` 依赖** — AGENTS.md §9.2 禁止擅自引入新依赖。
    测试用 `asyncio.run()` 包装异步调用，对齐 `test_m2_parse_handler.py`
    风格。

## 已完成 checklist

- [x] `ai-service/app/schemas/reasoning.py` 新增 `RuleResult.llm_reviewed` 字段
- [x] `ai-service/app/modules/reasoner/llm_reviewer.py` 实现 `LLMReviewer` 类 +
      `build_review_prompt` 函数
- [x] `ai-service/tests/test_m3_llm_reviewer.py` 编写 22 个测试用例（8 个测试类）
- [x] M3.02 + M3.01 共 42 个测试全部通过
- [x] 全 L3 共 315 个测试全部通过（M1 + M2 + M3 回归无破坏）
- [x] 模块覆盖率：`llm_reviewer.py` 92% ≥ 80% 门槛
- [x] ruff / black 全部通过
- [x] `docs/progress/m3.md` 更新 M3.02 完成状态 + 交付说明
- [x] 验收标准对照全部 ✅

## 测试用例分布

| 测试类 | 用例数 | 覆盖场景 |
|---|---|---|
| `TestBuildReviewPrompt` | 4 | prompt 含规则名/数值/上下文/截断 |
| `TestReviewTriggerConditions` | 4 | INFO/CRITICAL 跳过，WARN/ERROR 触发 |
| `TestReviewSuccess` | 4 | note 回填、is_explained=false、围栏解析、参数透传 |
| `TestReviewFallback` | 5 | 非 JSON/异常/空 reason/超时/单条不影响其他 |
| `TestImmutability` | 1 | 原 CheckResult 不被修改 |
| `TestIntegrationWithRuleEngine` | 2 | 故意不平衡财报端到端 + 混合 CheckResult |
| `TestConfidencePreserved` | 1 | confidence 复核前后一致 |
| `TestSerialization` | 1 | to_dict 含 llm_reviewed 字段 |

## 发现的风险

- **真实 7B 复核延迟未实测** — 测试用 `_StubHub` mock，真实 7B 在 RTX 4050
  Mobile 6GB VRAM 上的复核延迟待 M3.10 端到端测试时验证（spec §12.1
  REASON 链路 60s 超时，单规则目标 < 30s）。
- **prompt 上下文截断可能丢失关键科目** — 每表取前 15 个科目，若失败规则
  涉及的科目排在 15 名之外，LLM 看不到完整上下文。M3.10 端到端测试时若发现
  复核质量低，可改为"失败规则相关科目优先 + 其余截断"策略。
- **`is_explained=false` 的语义模糊** — LLM 自评"无法解释"可能保守或激进，
  M3.04 落表后需在前端展示该字段，让审计人员判断是否人工排查。
- **未实现 LLM 复核结果缓存** — 同一 PDF 重传时硬编码规则会命中 M2.10
  抽取缓存，但 LLM 复核会重复调用 7B。后续可考虑按
  `fin:cache:llm_review:{pdfMd5}:{ruleType}` 缓存复核结果。

## 下一步行动项

1. **M3.03 异常检测** — 实现 `AnomalyDetector`（同比/环比/逻辑异常），产出
   `Anomaly` 列表填充 `CheckResult.anomalies`，复用 `LLMReviewer` 复核
   逻辑异常（如应收账款激增但营收下滑）。
2. **M3.04 L2 写 accounting_check + anomaly 表** — 实现 `CheckWriter`
   消费 `CheckResult`，按 `llm_reviewed` 字段区分 note 来源；编排
   `RuleEngine.check` → `LLMReviewer.review` → `CheckWriter.write` 链路。
3. **M3.10 端到端 SLA 测试** — GPU 环境实测真实 7B 复核延迟，验证
   spec §12.1 REASON 链路 SLA。
4. **prompt 调优** — 收集 M3.10 端到端测试中 LLM 复核不准确的案例，迭代
   system prompt 和 few-shot 示例（当前未加 few-shot 避免 prompt 过长）。
