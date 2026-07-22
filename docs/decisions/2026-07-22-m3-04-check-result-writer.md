# 2026-07-22 M3.04 勾稽结果写入器与审查修复

## 背景

M3.01–M3.03 已完成 L3 Reasoner 三层能力（规则引擎 / LLM 复核 / 异常检测），
但 L3 CHECK progress 携带的 `CheckResult.to_dict()` payload 还需要落库到
`accounting_check` + `anomaly` 两张表，供前端勾稽页 / 异常页查询展示。

plan M3.04 验收标准：

* `accounting_check` 表写入 3 条规则结果
* `anomaly` 表写入异常列表
* 失败不阻断状态机推进（spec §8.4）

实现完成后做了全面代码审查，发现 3 个 Blocker + 3 个 Major + 5 个 Minor +
若干 Nit，全部修复后通过 L2 288 测试 + L3 359 测试。

## 决策列表

1. **失败策略对齐 `StatementWriter`** — 写库失败只 log error 不抛异常，
   避免阻断状态机推进；REPORT 阶段会因数据缺失自然失败并暴露问题
   （spec §8.4 "失败不强制回滚，保留数据供排查"）。这样保持 M2.08 已稳定的
   CHECK→REPORT 路径不被回滚。`writeCheckResult` 返回 `Mono<Integer>`
   （写入行数），失败时返回 0。

2. **事务边界（B1 修复）** — `accounting_check` + `anomaly` 两张表在
   同一事务内顺序写入（spec §8.4 任务边界 = 事务边界）；任一表写入失败
   整体回滚，避免半成品数据。通过 `TransactionalOperator.transactional()`
   包裹 `saveRules.flatMap(saveAnomalies)` 顺序链实现。

   选顺序链而非 `Mono.zip` 并行的原因：3 条 rule + N 条 anomaly 规模下
   顺序执行毫秒级完成，事务内并行 save 可能产生连接池压力且无显著收益。
   原实现用 `Mono.zip` 既无事务边界又无性能收益，是审查 Blocker B1。

3. **reconcile 路径不补写（B2/B3 修复）** — 进程崩溃发生在
   `step.setStatus(SUCCESS)` 之后、`writeCheckResult` 完成之前时，
   MQ 重放 CHECK SUCCESS 只调度 REPORT，不补写 check 结果。CHECK 链路
   无等价于 `ExtractCacheService` 的结果缓存，重放时拿不到原 result。
   此场景下 REPORT 会因数据缺失失败，由用户重传 PDF 触发整体重跑恢复
   （spec §8.4）。

   原实现有 `ensureCheckResultWritten` 方法 + 6 个测试，但
   `TaskOrchestrator.reconcileSuccessfulStep` CHECK 分支从不调用它，
   是死代码（审查 Blocker B2）。删除方法 + 测试，Javadoc 显式说明
   reconcile 不补写的设计决策。

4. **anomaly_type 白名单（M2 修复）** — `parseAnomaly` 校验
   `anomaly_type ∈ {yoy_change, qoq_change, logic_conflict}`，非白名单值
   跳过写库并 log warn，避免脏数据污染 `anomaly` 表。白名单对齐
   spec §2.3 M8 + L3 `AnomalyType` 枚举。

5. **payload 契约对齐 L3 `CheckResult.to_dict()`** — `parseCheckResult`
   严格对齐 L3 输出格式：`rules` 是 `List<Map>`，每条含 `rule_type` /
   `rule_name` / `expected` / `actual` / `diff` / `is_pass` / `severity` /
   `note`；`anomalies` 同理。Decimal 字段同时支持 String 和 Number 输入
   （Number 用 `toString()` 中转避免 double 精度损失，对齐
   `StatementWriter.bigDecimalOrNull` 实现）。

6. **rules 缺失视为无效 payload** — CHECK 阶段至少应产出 3 条规则结果；
   `rules` 字段缺失、非 List、或为空列表都返回 null 跳过写库。`anomalies`
   缺失视为空列表（M3.01 阶段 L3 还未集成 AnomalyDetector 时合法）。

7. **reportId 解析双路径** — 优先用 `task.ref_report_id`（M2.08 已稳定
   路径），缺失时回退到 `report.task_id` 关联查询。两条路径都失败时
   log error 返回 0，不阻断状态机。

8. **`ParsedCheckResult` 用 record** — 内部解析结果容器用 Java record
   （AGENTS.md §3.1 DTO 用 record），不可变且自动生成 accessor。

9. **`llm_reviewed` 字段不落表** — `accounting_check` 表无
   `llm_reviewed` 列（M3.04 决策：避免 schema 膨胀）；L2 不需要区分
   note 来源，note 字段已含 LLM 复核标记（"[LLM 复核] ..."）。

10. **`AnomalyDetector` docstring 对齐实现（M1 修复）** — 原模块 docstring
    承诺"对比期为 0 且本期非零时直接构造 ERROR 异常（科目新增/转出）"，
    但实现是直接 `continue` 跳过。修改 docstring 对齐实现：对比期为 0
    跳过，科目新增/转出（对比期无此科目）由调用方在 M3.04 编排层另行
    处理。理由：

    * 测试 `test_previous_zero_skipped` 明确验证"跳过"行为，与实现一致
    * "本期非零而对比期为零"语义模糊（可能是科目新增、也可能是科目转出），
      单纯 ERROR 异常可能误报很多
    * 实际更准确的处理在调用方：M3.04 编排层可按"对比期无此科目且本期非零"
      做单独的科目新增异常（M3.04 未实现，留待 M3.10 端到端验证后按需补）

## 已完成 checklist

* 新增 `AccountingCheck` / `AnomalyRecord` 实体（Lombok @Data @Builder）
* 新增 `AccountingCheckRepository` / `AnomalyRepository`
  （`ReactiveCrudRepository`）
* 新增 `CheckResultWriter` 服务（parse + persist + resolve reportId）
* `TaskOrchestrator` 注入 `CheckResultWriter`，CHECK 首次 SUCCESS 调用
  `writeCheckResult` 后再触发 REPORT
* 23 个 `CheckResultWriterTest` 测试用例（ParseCheckResult / WriteCheckResult
  / Transaction 三个嵌套类）
* 修复 B1（事务边界）/ B2（死代码）/ B3（reconcile 缓存缺失）/ M1（docstring）
  / M2（anomaly_type 白名单）/ M3（anomalyRepo 失败测试）/ m2-m5（死代码
  与过时注释）/ n4（record）
* 更新 `docs/progress/m3.md` M3.04 交付说明
* L2 全部 288 测试通过 / L3 全部 359 测试通过

## 发现的风险

1. **CHECK reconcile 不补写** — 进程崩溃发生在 `step.setStatus(SUCCESS)`
   之后、`writeCheckResult` 完成之前时，REPORT 会因数据缺失失败，需要
   用户重传 PDF 触发整体重跑。M2.08 的 EXTRACT 链路有 `ExtractCacheService`
   缓解此问题，CHECK 链路无等价物。M3.10 端到端测试需观察此场景的实际
   发生频率，必要时为 CHECK 增加 result 缓存（与 EXTRACT 同模式）。

2. **`anomaly_type` 白名单是硬编码** — 若 spec §2.3 M8 后续扩展异常类型
   （如同业对比异常），需同步更新 `CheckResultWriter.VALID_ANOMALY_TYPES`
   和 L3 `AnomalyType` 枚举。建议在 spec 变更 PR 中检查此处。

3. **`llm_reviewed` 不落表** — 若后续需要按"是否经 LLM 复核"筛选
   accounting_check 记录（如审计报告），需要 schema 变更加列。当前
   note 字段含 `[LLM 复核]` 前缀可做模糊匹配，但不如独立列高效。

## 下一步行动项

* M3.05 L3 M10 ReportGenerator NLG — 生成 Markdown 报告
* M3.10 端到端 SLA 测试 — 真实 7B 模型 + 真实茅台年报验证
  `CheckResultWriter` 在真实 L3 payload 下的写入正确性
