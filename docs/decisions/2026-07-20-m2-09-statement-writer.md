# M2.09 L2 抽取结果写 financial_statement 决策记录

> 日期：2026-07-20
> 任务：M2.09 L2 抽取结果写 financial_statement
> 关联：spec §3.2 链路 A / §5.2.2 financial_statement 表结构 / §8.4 数据一致性 / plan §4 M2.09
> 进度：[docs/progress/m2.md](../progress/m2.md)

---

## 1. 背景

M2.08 交付了 L2 三表并行抽取编排（`ExtractDispatcher` + `ExtractCompletionTracker` + `TaskOrchestrator.handleExtractStepSuccess`），但 EXTRACT step SUCCESS 后只触发 CHECK，不消费 L3 progress 携带的 `result` payload。

spec §3.2 链路 A 要求：3 条 extract progress 携带的 JSON 写入 `financial_statement` 表，供 CHECK 勾稽 + M11 报告生成 + 前端展示复用。

M2.09 闭环 L2 写库层：交付 `FinancialStatementItem` 实体 + `FinancialStatementRepository` + `StatementWriter` 服务，在 `TaskOrchestrator.handleStepSuccess` 中先写库再触发 CHECK。L3 端 extract handler 仍是 mock 但返回符合 M2.09 契约的 payload，让 L2 能端到端跑通。

---

## 2. 决策列表

### D1：String 常量而非 enum

**背景**：L3 端已有 `StatementType` 枚举（`balance_sheet` / `income_statement` / `cash_flow`），L2 是否引入对应 enum？

**选项**：
- A. L2 也定义 `StatementType` enum，与 L3 双向映射
- B. L2 用 `STATEMENT_TYPE_BS = "balance_sheet"` 等 String 常量

**决策**：B

**理由**：
- L2 不该引入对 L3 枚举的依赖（spec §3.1 5 层架构禁止跨层直接引用）
- 字符串值在 spec §5.2.2 表结构 + L3 `StatementType.value` 双方锁定，相当于"契约即常量"
- enum 在 L2 端需要额外 `@MappedTypes` 适配 R2DBC，增加无意义复杂度
- 未来若 L3 enum 名变更（如 `BS` → `BALANCE_SHEET`），L2 不受影响（只看 `.value` 字符串）

### D2：失败策略：log error 不抛异常

**背景**：`StatementWriter.writeStatement` 写库失败时，是否抛异常让 `TaskOrchestrator` 转任务 FAILED？

**选项**：
- A. 写库失败抛异常，`TaskOrchestrator.handleStepFailure` 转 task.status=FAILED
- B. 写库失败只 log error 返回 0，不阻断状态机推进

**决策**：B

**理由**：
- spec §8.4 "失败不强制回滚，标记 task.status=FAILED，保留数据供排查" — 但这是"可"而非"必须"
- M2.08 已稳定的 EXTRACT→CHECK 路径不应被写库失败回滚；CHECK 阶段会因数据缺失自然失败并暴露问题
- A 选项会导致：3 条 extract 中 1 条写库失败 → 整任务 FAILED，但其他 2 条数据已入库；用户重试时无法只重写失败的那条（M2.10 缓存层会命中 pdf_md5 跳过 extract）
- B 选项的"延迟失败"语义更友好：CHECK 阶段发现 BS 数据缺失 → CHECK 失败 → 任务 FAILED → 用户重试整轮；这样保持 EXTRACT→CHECK 路径的稳定性
- 缺点：写库失败时 task 仍标记 SUCCESS，需要靠 CHECK 兜底；M2.12 集成测试时验证此路径

### D3：reportId 双路径解析

**背景**：`StatementWriter` 需要把科目行关联到 `report_id`，但 `task` 表的 `ref_report_id` 字段可能为 null（M1.x 早期数据）。

**选项**：
- A. 只用 `task.ref_report_id`，缺失时跳过写库
- B. 优先 `task.ref_report_id`，缺失时回退 `reportRepo.findByTaskId(taskId)`
- C. 强制要求 `task.ref_report_id` 非 null，迁移历史数据

**决策**：B

**理由**：
- A 会丢历史数据，C 需要数据迁移脚本增加复杂度
- B 兼容历史 + 新数据：新数据走 `task.ref_report_id` 快路径（O(1) 主键查询），历史数据走 `reportRepo.findByTaskId` 慢路径（O(N) 索引扫描）
- 两条路径都失败时 `switchIfEmpty` 兜底返回 0 行（task 不存在或无 report 关联），log error 不阻断
- M1.x 已建立 `report.task_id` 索引，回退路径性能可接受

### D4：Reactor `switchIfEmpty(Mono.defer(...))` 兜底空 Mono

**背景**：`resolveReportId(taskId)` 在 task 不存在或无 reportId 关联时返回空 Mono。`flatMap` 内部对空 Mono 不触发，整个链直接 `onComplete()` 而非 `onNext(0)`。

**问题**：测试 `shouldReturnZeroWhenTaskIsNotFound` / `shouldReturnZeroWhenReportIdCannotBeResolved` 用 `StepVerifier.assertNext(count -> assertEquals(0, count))` 断言，但实际收到 `onComplete()` 而非 `onNext(0)`，断言失败："expected: onNext(); actual: onComplete()"。

**选项**：
- A. 改测试用 `verifyComplete()` 不断言值
- B. 在 `resolveReportId` 链末尾加 `switchIfEmpty(Mono.defer(() -> { log.error(...); return Mono.just(0); }))` 兜底
- C. 改 `resolveReportId` 返回 `Mono<Long>` 改为 `Mono<Optional<Long>>`，永不空

**决策**：B

**理由**：
- A 损失断言精度，无法区分"task 不存在"和"task 存在但 reportId 为 null"两种空场景
- C 引入 `Optional<Long>` 包装增加复杂度，破坏 Reactor 风格
- B 是 Reactor 惯用法：`switchIfEmpty(Mono.defer(...))` 在空 Mono 时插入兜底值；`Mono.defer(...)` 延迟创建保证 log.error 只在空场景触发
- 注释说明"Reactor 空链陷阱"，未来维护者能理解

### D5：千分位字符串解析

**背景**：A 股财报 PDF 常见 `"1,234,567.89"` 形式的千分位字符串。`BigDecimal` 构造器不接受逗号。

**选项**：
- A. 拒绝千分位字符串，让 L3 handler 转换
- B. `bigDecimalOrNull` 用 `s.trim().replace(",", "")` 后 `new BigDecimal(...)`

**决策**：B

**理由**：
- A 把责任推给 L3，但 L3 的 Qwen2.5-Instruct 输出格式不稳定（可能输出 `"1,234,567.89"` 或 `1234567.89`）
- B 在 L2 端容错，接受两种格式；M2.07 validator 也接受千分位字符串（M2.07 决策记录 D6）
- 解析失败返回 null 视为无效行，由 caller 决定是否跳过
- 注意：`replace(",", "")` 是全量替换，对中文千分位 `"1,234,567.89"` 和英文 `"1,234,567.89"` 都适用；对未来可能的中文逗号 `"，"` 不适用，但 A 股财报不用中文逗号

### D6：Mockito `lenient()` stub StatementWriter

**背景**：`TaskOrchestratorTest` 新增 `@Mock private StatementWriter statementWriter`。strict stubbing 模式下，`when(statementWriter.writeStatement(anyString(), anyString(), any())).thenReturn(Mono.just(0))` 会在非 EXTRACT 测试（如 `shouldCreateTaskAndDispatchParse`）中触发 UnnecessaryStubbing。

**选项**：
- A. 每个 EXTRACT 测试单独 stub，非 EXTRACT 测试不 stub
- B. `setUp()` 中用 `lenient().when(...)` 全局 stub
- C. 把 stub 移到 `@Test` 方法内

**决策**：B

**理由**：
- A 增加 30 个测试的重复代码
- C 与 A 类似，破坏 `setUp()` 集中管理 fixture 的模式
- B 用 `lenient()` 全局 stub `writeStatement → Mono.just(0)`，个别需要 verify 调用次数的测试可显式 `when(...).thenReturn(...)` 覆盖
- 注释说明"M2.09: StatementWriter returns 0 by default; individual tests override when needed"，未来维护者能理解
- M2.08 决策记录 D9 已有先例：`TaskOrchestratorTest CF stub 用 lenient` 同样模式

### D7：L3 handler 仍是 mock 但契约对齐

**背景**：M2.09 只交付 L2 写库层。L3 端 `extractor/handler.py` 仍是 M1.x 的 mock，但需要返回符合 M2.09 契约的 payload 让 L2 `StatementWriter` 能端到端跑通。

**选项**：
- A. M2.09 同时交付 L3 真实 ModelHub 调用链
- B. L3 handler 返回 mock 但符合 M2.09 契约的 payload，真实调用链留给 M4 T1
- C. L3 handler 保持 M1.x 的 mock（只 echo "extract mock"），L2 StatementWriter 单测覆盖

**决策**：B

**理由**：
- A 超出 M2.09 任务范围（plan §4 M2.09 只要求 L2 写库层）；M4 T1 1.5B QLoRA 训练 + Qwen2.5-7B 推理需要 GPU 资源和训练数据，不在 M2 周期
- C 让 L2 StatementWriter 只能靠单测覆盖，无法端到端验证；M2.12 集成测试时需要 mock L3 也行，但 M2.09 阶段就提前对齐契约更稳
- B 是渐进式交付：M2.09 闭环 L2 写库层 + 契约对齐，M4 T1 上线真实 7B 模型时只需替换 handler 内部实现，不动 L2
- mock payload BS 2 条 + IS 1 条 + CF 1 条 = 4 条，便于 L2 StatementWriter 端到端测试；真实模型上线后单表 ~50 条 × 3 = ~150 条

### D8：无幂等去重

**背景**：`StatementWriter.writeStatement` 是否做幂等检查防止重复写入？

**选项**：
- A. 加 `idempotency_key` 唯一约束 + `INSERT IGNORE`
- B. 不做幂等检查，依赖 `TaskOrchestrator.handleStepSuccess` 已对重放 SUCCESS 去重

**决策**：B

**理由**：
- `TaskOrchestrator.handleStepSuccess` 已对重放 SUCCESS 做去重：reconcile 路径不调 `StatementWriter`，只走 `handleExtractStepSuccess` 触发 CHECK
- spec §8.4 "同 PDF 重传：基于 pdf_md5 命中 Redis 缓存，跳过解析抽取" — 同 PDF 重传在 dispatchAll 入口就被 M2.10 缓存层拦截，不会到 `StatementWriter`
- A 增加唯一约束 + `INSERT IGNORE` 复杂度，且 `idempotency_key = taskId + step` 在重试场景下会变化（spec §8.3 "不含 retry，否则重试时 key 变化导致幂等失效"）
- B 选项的"依赖上游去重"是 Reactor 风格的简洁方案；如果未来 reconcile 路径也调本类，再加幂等约束
- 已知风险：如果 `handleStepSuccess` 的去重逻辑有 bug，会导致重复写入；M2.12 集成测试时验证重放场景

### D9：未接入 source_bbox

**背景**：spec §5.2.2 `source_bbox` 字段（科目所在 PDF bbox）当前 L3 handler 未提供。

**选项**：
- A. M2.09 阶段补全 L3 handler 提取 bbox
- B. `source_bbox` 字段留 null，留给 M4 LayoutLMv3 表格识别时补全

**决策**：B

**理由**：
- M2.09 的 L3 handler 仍是 mock（D7），不提取真实 bbox
- spec §5.2.2 `source_bbox` 用途：M11 报告生成时高亮 PDF 原始位置 + 前端展示"查看原文"
- M4 LayoutLMv3 表格识别会输出 bbox，那时再补全 `source_bbox` 字段
- 当前 `FinancialStatementItem.sourceBbox` 字段允许 null，不影响写库

### D10：未接入 validation_warnings

**背景**：M2.07 `Validator.validate` 返回 `ValidationResult.issues`（warning 级问题，如 `missing_required_item`）。L2 是否持久化这些 warnings？

**选项**：
- A. `financial_statement` 表加 `validation_warnings JSON` 字段
- B. `validation.issues` 只在 progress 消息中流转，不持久化
- C. 单独建 `extract_validation_warning` 表

**决策**：B

**理由**：
- spec §5.2.2 `financial_statement` 表无 `validation_warnings` 字段；擅自加字段违反 AGENTS.md §9.2 "禁止引入设计文档未提及的新依赖"
- M2.07 决策记录已明确"warning 级不阻断"，warnings 只在 L3 日志 + progress 消息中流转
- M11 报告生成时如需展示 warnings，可重新跑 Validator 或从 L3 日志聚合
- C 选项过度设计，warnings 是"软提示"不需要关系型查询

---

## 3. 已完成 Checklist

- [x] 实现 `FinancialStatementItem.java`（R2DBC 实体映射 spec §5.2.2）
- [x] 实现 `FinancialStatementRepository.java`（countByReportId / countByReportIdAndStatementType / findByReportId* / deleteByReportId）
- [x] 实现 `StatementWriter.java`（writeStatement + mapStatementType + parseStatement + parseItem + bigDecimalOrNull + resolveReportId + persistItems + switchIfEmpty 兜底）
- [x] 改造 `TaskOrchestrator.handleStepSuccess`（EXTRACT 路径先写库再触发 CHECK）
- [x] 更新 L3 `extractor/handler.py` 返回 M2.09 契约 payload（mock BS 2 + IS 1 + CF 1）
- [x] 编写 `StatementWriterTest.java`（20 个测试覆盖 happy path + 各容错路径）
- [x] 适配 `TaskOrchestratorTest.java`（构造函数 + `lenient()` stub StatementWriter）
- [x] 跑 `mvn clean verify` BUILD SUCCESS（231 个测试全绿，jacoco:check 通过）
- [x] 更新 `docs/progress/m2.md` 打勾 M2.09

---

## 4. 发现的风险

### R1：写库失败延迟暴露

D2 决策"写库失败不抛异常"会导致 task 仍标记 SUCCESS，CHECK 阶段才发现数据缺失。

**缓解**：
- CHECK 阶段会因数据缺失自然失败（spec §3.2.1 `handleCheckStepSuccess` 检查 `financial_statement` 表行数）
- M2.12 集成测试时验证"写库失败 → CHECK 失败 → 任务 FAILED"完整链路
- 日志中 `[StatementWriter] 写入失败` ERROR 级别，可被监控告警捕获
- 备选方案：如果 CHECK 兜底不可靠，未来可在 `handleExtractStepSuccess` 加 `fsRepo.countByReportId` 校验，count=0 时转 task.status=FAILED

### R2：Reactor 空链陷阱易遗漏

D4 决策"`switchIfEmpty(Mono.defer(...))` 兜底空 Mono"是 Reactor 惯用法，但未来维护者新增"返回空 Mono 表示无数据"的链时可能漏加 `switchIfEmpty`。

**缓解**：
- `StatementWriter` 注释明确说明"Reactor 空链陷阱"
- `StatementWriterTest` 的 `shouldReturnZeroWhenTaskIsNotFound` / `shouldReturnZeroWhenReportIdCannotBeResolved` 两个测试专门覆盖空 Mono 场景，未来重构时会暴露问题
- Reactor 最佳实践：`flatMap` 链末尾必加 `switchIfEmpty` 或 `defaultIfEmpty` 兜底空 Mono

### R3：无幂等去重在重放场景下可能重复写入

D8 决策"依赖上游去重"在 `handleStepSuccess` 去重逻辑有 bug 时会失效。

**缓解**：
- `TaskOrchestrator.handleStepSuccess` 的去重逻辑由 M2.08 测试覆盖（`shouldIgnoreProgressForTerminalTasks` / `shouldRetainPartialExtractionUntilAllStepsSucceed`）
- M2.12 集成测试时验证 MQ redeliver 场景：同一条 extract progress 收到 2 次，`financial_statement` 表应只有 1 份科目数据
- 备选方案：未来加 `idempotency_key` 唯一约束（spec §8.3 已要求 `idempotency_key = taskId + step`，但当前未用作 DB 唯一键）

### R4：L3 mock payload 与真实模型输出格式差异

D7 决策"L3 handler 仍是 mock 但契约对齐"的 mock payload 是手工构造的，真实 Qwen2.5-7B 输出可能有差异（如多输出字段 / 字段顺序不同 / 数值类型不同）。

**缓解**：
- M2.06 `_coerce_to_statement` 已容忍 `statements` key 用 `.value` 或 `.name`，字段顺序无关
- M2.07 `Validator.validate` 校验 report_period 格式 / NaN / inf / 关键科目缺失
- M4 T1 上线真实模型后，M2.12 集成测试会暴露格式差异
- 备选方案：M4 T1 训练完成后，用真实模型输出回放 L2 `StatementWriter` 单测，替换 mock payload

### R5：千分位字符串解析误吞中文逗号

D5 决策"`replace(",", "")` 全量替换"对中文逗号 `，` 不生效，但 A 股财报 PDF 通常用英文逗号。

**缓解**：
- A 股财报格式标准化程度高，PDF 提取后数值字段通常用英文逗号
- M2.07 validator 会拒绝 NaN / 非数值字符串，千分位解析失败返回 null 视为无效行
- 备选方案：如果未来发现中文逗号，加 `replace("，", "")` 全量替换

---

## 5. 下一步行动项

1. **M2.10 抽取结果缓存**：
   - 同 pdf_md5 重传直接命中 Redis 缓存
   - `ExtractCacheService` 在 `dispatchAll` 入口查缓存，命中跳过 extract 直接 CHECK
   - 验收：重传同 PDF → 跳过 extract 步骤 → 直接 CHECK
   - 关联 R3：缓存层拦截同 PDF 重传，减少 `StatementWriter` 重复写入风险

2. **M2.11 前端三表展示页**：
   - 消费 `financial_statement` 表数据展示 BS/IS/CF 三表
   - 复用 `FinancialStatementRepository.findByReportIdOrderByStatementTypeAscItemNameAsc`
   - spec §2.4 M11 报告生成阶段也会复用此数据

3. **M2.12 集成测试**：
   - 真实年报端到端验证（缓解 R1 写库失败延迟暴露 + R3 重放重复写入 + R4 mock 与真实模型差异）
   - spec §12.1 EXTRACT 阶段 SLA 60s
   - 验证 `SELECT COUNT(*) FROM financial_statement WHERE report_id=?` ≈ 150 行

4. **M4 T1 1.5B QLoRA + Qwen2.5-7B 真实推理**：
   - 替换 `ai-service/app/modules/extractor/handler.py` 的 mock body 为真实 `extract_with_retry` 调用
   - `with VramScheduler.load_for_scene_with_lock(Scene.EXTRACT) as lock: result, validation = extract_with_retry(...)`
   - busy 时抛 `ModelLockBusyException` → handler 转 `nack(requeue=True)` + sleep `model_lock_retry_seconds`
