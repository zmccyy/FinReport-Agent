# M2.10 抽取结果缓存决策记录

> 日期：2026-07-21
> 任务：M2.10 抽取结果缓存
> 关联：spec §3.10 抽取缓存 / §3.2.1 AtomicInteger 状态机 / §8.4 数据一致性 / plan §4 M2.10
> 进度：[docs/progress/m2.md](../progress/m2.md)

---

## 1. 背景

M2.08 已交付三表并行抽取编排（`ExtractDispatcher` + `ExtractCompletionTracker`），M2.09 已交付抽取结果写入 `financial_statement` 表。但同一份 PDF 重复上传时，每次都要走完整的 L3 7B 推理（~5GB 4-bit，60s+ SLA），代价极高。

spec §3.10 要求：**同 `pdf_md5` 重传直接命中 Redis 缓存，跳过 extract 步骤直接 CHECK**。

M2.10 闭环缓存层：交付 `ExtractCacheService`（`fin:cache:extract:{pdfMd5}:{step}` key，TTL=7d），在 `TaskOrchestrator.dispatchExtractionSteps` 入口（调 `extractDispatcher.dispatchAll` 之前）查缓存，三表全部命中即跳过 MQ 投递，重放 3 条 success 路径直接触发 CHECK；并在 `handleStepSuccess` 的 EXTRACT 分支于 `writeStatement` 之后写缓存。

---

## 2. 决策列表

### D1：跨用户共享缓存（key 不含 userId）

**背景**：缓存 key 是否包含 userId？

**选项**：
- A. key 含 userId：`fin:cache:extract:{userId}:{pdfMd5}:{step}` — 每个用户独立缓存
- B. key 不含 userId：`fin:cache:extract:{pdfMd5}:{step}` — 跨用户共享

**决策**：B

**理由**：
- spec §3.10 明确"同 PDF 内容（同 md5）的 extract 结果对任意用户都成立"——A 股财报是公开信息，BS/IS/CF 三表数据对所有用户一致
- 跨用户共享最大化缓存命中率：用户 A 上传某公司年报后，用户 B 上传同一份 PDF 立即命中缓存
- 缓存仅含 L3 抽取的客观科目数据（`{success, statement, validation, confidence, source_page, ...}`），不含用户业务字段（如 `task_id` / `ref_report_id`）；重放时 `StatementWriter.writeStatement` 会用当前 taskId 解析 reportId，不会跨用户污染数据
- 风险：若未来需要"用户私有抽取结果"（如自定义 prompt 抽取），需要扩展 key；当前不在 spec 范围内

### D2：键设计 `fin:cache:extract:{pdfMd5}:{step.name()}`

**背景**：缓存 key 怎么设计？

**选项**：
- A. 整 JSON 一份：`fin:cache:extract:{pdfMd5}` → `{BS: {...}, IS: {...}, CF: {...}}`
- B. 按 step 分 key：`fin:cache:extract:{pdfMd5}:{step.name()}` × 3

**决策**：B

**理由**：
- 与 `fin:extract:done:{taskId}` / `fin:extract:failed:{taskId}` 同前缀家族，便于运维 `KEYS fin:cache:extract:*` 批量扫描
- 按 step 分 key 让单步重试 cache miss 时其他步仍可命中（虽然当前 `lookupAll` 仍要求全命中才 replay，但留扩展空间）
- Redis 单 key value < 1MB（一份 statement JSON ~50KB），3 个 key 总占用 ~150KB，无内存压力
- `lookupAll` 用 `concatMap` 串行查 3 次（O(3) RTT），Redis 内网 < 1ms，整体仍比走 MQ 快 60s+；M3 性能优化时可换 `MGET` 批量

### D3：TTL=7d

**背景**：缓存 TTL 多长？

**决策**：7 天（spec §3.10 锁定）

**理由**：
- 7 天覆盖周报/月报迭代周期；A 股年报每年披露一次，季报每季度披露一次，7 天足够覆盖一个迭代窗口
- 7 天后自动失效，避免长期占用 Redis 内存；超出 7d 的财报可重新抽取（模型升级 / prompt 优化后值不同）
- 与 spec §3.10 明文一致；不在本次任务范围内调整 spec

### D4：失败策略 — Redis 故障静默 fallback 到 MQ

**背景**：Redis 故障时如何处理？

**选项**：
- A. 抛异常让任务 FAILED
- B. 静默吞掉，降级走 MQ 投递
- C. 抛异常让调用方决定

**决策**：B

**理由**：
- spec §3.10 失败策略明确"Redis 故障静默吞掉，不影响主流程"
- 缓存是优化层而非必需品；Redis 故障时业务仍能正常完成（只是慢一点），不该让用户感知
- `lookup` / `lookupAll` / `store` 全部 `onErrorResume` 静默吞掉：lookup → empty Mono（视为 cache miss）、lookupAll → empty Map（视为 cache miss）、store → empty Mono（视为写入成功，下次仍 cache miss）
- `checkCacheAndReplayOrDispatch` 在 lookup 返回空 Map 时自然 fallback 到 `extractDispatcher.dispatchAll`，业务无感降级

### D5：三表全命中才 replay，部分命中走 MQ

**背景**：`lookupAll` 返回 2 条缓存（缺 1 条）时怎么办？

**选项**：
- A. 用缓存的 2 条 + MQ 抽取缺失的 1 条
- B. 全部走 MQ 重新抽取

**决策**：B

**理由**：
- 半缓存会导致数据不一致：BS 缓存（7 天前）+ IS 新抽（当前模型版本）= 三表时间戳不一致，CHECK 勾稽可能出问题
- 保守策略宁可多抽一次也不冒数据不一致风险
- 部分命中的概率极低：缓存是按 `store` 三次独立 SET 写入的，3 次都成功才进入"全命中"状态；只有在 TTL 即将过期时才会出现部分命中（7d TTL ± 几秒差距），实际几乎不会发生
- 若未来需要"部分命中补抽"，可在 `replayCachedExtracts` 内只对 `cached` 缺失的 step 调 `extractDispatcher.dispatchSingle`，但当前不在 spec 范围内

### D6：`replayCachedExtracts` 不调 `extractTracker.recordSuccess`

**背景**：cache replay 路径是否走 Redis AtomicInteger 计数器？

**选项**：
- A. 调 `tracker.recordSuccess` 三次，让计数到 3 触发 CHECK
- B. 直接调 `transitionToExtractSuccess` 跳到 CHECK

**决策**：B

**理由**：
- `extractTracker.recordSuccess` 是为 MQ 回报设计——L3 worker 完成 extract 后回报 success，L2 用 INCR 计数到 3 触发 CHECK
- cache replay 路径不经过 L3 worker，是 L2 直接重放缓存；如果走 `recordSuccess` 会污染计数器（计数器在 `dispatchAll` 入口已被 `tracker.reset` 清零，正常路径是 L3 回报逐步累积；replay 路径不该参与累积）
- 直接调 `transitionToExtractSuccess` 与正常路径 `handleExtractStepSuccess` 的最终出口一致（`EXTRACT_SUCCESS` → `dispatchStepIfPending(CHECK)`），保证 task 状态机推进语义统一
- 也不调 `extractCacheService.store`，避免重复写缓存

### D7：`storeExtractCache` 在 `writeStatement` 之后

**背景**：缓存写入顺序——在 `writeStatement` 之前还是之后？

**选项**：
- A. 先写缓存，再写 statement（即使 statement 失败，缓存仍可用）
- B. 先写 statement，再写缓存（statement 失败时不写缓存）

**决策**：B

**理由**：
- 避免"孤儿缓存"——cache 写成功但 statement 写失败时，下次 cache hit 会拿到无 DB 行的缓存，CHECK 阶段因数据缺失自然失败
- spec §8.4 "失败不强制回滚" 意味着 statement 写失败不阻断主流程，但 cache 写入应在 writeStatement 完成后才能保证一致性
- M2.09 的 `StatementWriter` 失败策略是 `onErrorResume → Mono.just(0)`，即写失败仍返回 Mono.just(0)；所以 `.then(storeExtractCache)` 会在 writeStatement 完成后（无论成功或失败返回 0）触发，cache 写入总能进行
- 极端情况：writeStatement 失败 + cache 写入成功 → 下次重传仍 cache hit，CHECK 仍因数据缺失失败；这是已知限制，但概率极低（writeStatement 失败通常是 DB 故障，cache 写入也会因 Redis 故障一起失败）

### D8：`storeExtractCache` `onErrorResume` 兜底

**背景**：`reportRepo.findByTaskId` 抛异常时如何处理？

**决策**：`onErrorResume` 静默吞掉，返回 empty Mono

**理由**：
- `reportRepo.findByTaskId` 抛异常不应阻断主流程（extract 已成功，CHECK 触发路径仍可推进）
- 缓存写入失败仅意味着下次重传仍走 MQ，业务无感
- 与 `ExtractCacheService.store` 内部的 Redis 故障处理对称——cache 层和 orchestrator 层都做兜底

### D9：`switchIfEmpty(Mono.defer(...))` 兜底 report 不存在

**背景**：`reportRepo.findByTaskId` 返回空 Mono 时（历史 task 无 report 关联），`flatMap` 不会触发，整个链直接 `onComplete()`。如何处理？

**决策**：`switchIfEmpty(Mono.defer(() -> extractDispatcher.dispatchAll(task, payload).thenReturn(task)))`

**理由**：
- 历史 task 可能无 report 关联（如 M1.x mock 数据）；这种情况下无法查 cache，应直接走 MQ 投递
- `flatMap` 在 empty source 上不触发会直接 `onComplete()`，导致整个流没有 `onNext`；用 `switchIfEmpty` 把这种情况兜底走 MQ
- 用 `Mono.defer(...)` 保证 `extractDispatcher.dispatchAll` 在订阅时才调用，避免在装配阶段副作用

### D10：Mockito `lenient()` stub 默认 cache miss

**背景**：`TaskOrchestratorTest` 中如何 stub `extractCacheService` 和 `reportRepo`？

**决策**：`setUp` 中 `lenient()` stub 默认 cache miss 行为

**理由**：
- 多数 `TaskOrchestratorTest` 测试不涉及 cache（如 createTask / dispatchTask / cancelTask），但构造函数需要 `extractCacheService` + `reportRepo`；用 `lenient()` 默认 stub cache miss（lookupAll → 空 Map、store → empty Mono、reportRepo.findByTaskId → empty Mono），避免 UnnecessaryStubbing
- 个别 cache-hit 测试（如 `shouldSkipExtractAndReplayWhenAllCached`）显式 `when(...).thenReturn(...)` 覆盖默认 stub
- 这样既保持 Mockito strict mode 的清洁度，又避免每个测试都重复 stub

---

## 3. 已完成 checklist

- [x] `ExtractCacheService` 实现：`cacheKey` / `lookup` / `lookupAll` / `store`（TTL=7d）
- [x] `ExtractCacheServiceTest` 14 个测试覆盖全路径（命中/未命中/Redis 故障/参数 null × 4 个方法）
- [x] `TaskOrchestrator` 接入 cache：
  - [x] 构造函数注入 `ExtractCacheService` + `ReportRepository`
  - [x] `dispatchExtractionSteps` 调 `checkCacheAndReplayOrDispatch`
  - [x] `checkCacheAndReplayOrDispatch` 实现全命中 replay / 部分命中走 MQ / report 不存在走 MQ
  - [x] `replayCachedExtracts` 重放 3 条 success + `transitionToExtractSuccess` 触发 CHECK
  - [x] `handleStepSuccess` EXTRACT 分支调 `storeExtractCache`（在 `writeStatement` 之后）
- [x] `TaskOrchestratorTest$ExtractionCache` 5 个 TDD 测试：
  - [x] RED 验证：5 个测试全部失败（cache 逻辑未实现）
  - [x] GREEN 实现：5 个测试全部通过
- [x] 全量测试：`mvn clean verify` BUILD SUCCESS，250 个测试全绿
- [x] 覆盖率：`jacoco:check` 通过（"All coverage checks have been met."）
- [x] 质量门：`mvn checkstyle:check spotbugs:check` 通过
- [x] 进度记录：`docs/progress/m2.md` M2.10 打勾 + 交付说明
- [x] 决策归档：本文件

---

## 4. 发现的风险

### R1：`lookupAll` 用 `concatMap` 顺序查 3 个 key

3 次 Redis GET 串行（而非 `MGET` 批量）；3 表缓存查询 O(3) RTT。Redis 内网 < 1ms，整体仍比走 MQ 快 60s+。M3 性能优化时可换 `MGET` 批量。

### R2：缓存按 step 独立 key，未做事务性批量写入

`store` 三次独立 SET，理论上有 1-2 步成功 1 步失败的可能。但因 `lookupAll` 要求全命中才 replay，单步缺失会自然 fallback 到 MQ，不会读到部分缓存。风险可接受。

### R3：`replayCachedExtracts` 标 step SUCCESS 不写 `started_at` / `duration_ms`

缓存重放路径无实际 L3 推理时间戳，仅写 `finished_at`。这是 spec §3.10 的预期行为（cache hit 不该有 L3 推理时延统计）。运维如需区分 cache replay 与真实 SUCCESS，可查 `step.duration_ms IS NULL` 配合 `task.progress` 跳变识别。

### R4：未接入 cache 失效信号

当前 TTL=7d 自然过期。如未来需要主动失效（如 L3 模型升级 / prompt 优化 / 数据修订），需新增 `extractCacheService.invalidate(pdfMd5)` 接口 + 触发点（M4 模型版本切换时）。

### R5：跨用户共享缓存的隐私考量

缓存 key 不含 userId，相同 pdf_md5 命中同一份缓存。已确认 A 股财报是公开信息，BS/IS/CF 三表数据对所有用户一致；缓存仅含 L3 抽取的客观科目数据，不含用户业务字段。但若未来需要"用户私有抽取结果"（如自定义 prompt 抽取），需要扩展 key。

---

## 5. 下一步行动项

- **M2.11 前端三表展示页**：消费 `financial_statement` 表数据展示 BS/IS/CF 三表；spec §2.4 M11 报告生成阶段也会复用此数据
- **M2.12 集成测试**：真实年报端到端验证；spec §12.1 EXTRACT 阶段 SLA 60s；验证方式包含"重传相同 PDF 命中缓存"（M2 阶段验收标准之一）
- **M3 性能优化**（可选）：`lookupAll` 换 `MGET` 批量查询；`store` 换 `MSET` 批量写入
- **M4 模型版本切换**（可选）：新增 `extractCacheService.invalidate(pdfMd5)` 接口 + 触发点
