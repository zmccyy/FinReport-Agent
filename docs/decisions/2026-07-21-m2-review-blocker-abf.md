# 2026-07-21 M2 代码审查 Blocker A/B/F 修复

## 背景

M2 review 第一轮（commit `4c607b4`）修复了 P0+P1 共 7 项关键问题，归档了 7 项未修复风险作为 M3 follow-up。本文档记录其中 3 个 Blocker（A/B/F）的修复决策——它们必须在 M3 modelhub 重构前完成，因为：
- **Blocker A** 影响 6GB VRAM 硬约束（spec §8.1）：超时线程不释放显存会导致后续推理 OOM
- **Blocker B** 破坏数据一致性（spec §8.4）：并发 replay 重复写 financial_statement 行
- **Blocker F** 破坏数据完整性：进程崩溃窗口期导致 statement 丢失，CHECK 阶段必然失败

修复分支：`fix/m2-review-blocker-abf`，基于 `main`（`4c607b4` 之后）。

## 决策列表

### D1. Blocker A — llm_loader 超时线程改 concurrent.futures + 主动 unload

**问题**：`ai-service/app/modules/modelhub/llm_loader.py` 的 `TransformersBackend.generate` 用 `threading.Thread(daemon=True)` + `join(timeout)` 实现超时。daemon 线程超时后仍持续占用 GPU 显存（`model.generate` 阻塞无法中断），原实现只调 `_reset()` 清空 Python 引用，不释放 CUDA cache，导致后续推理 OOM。

**修复**：
1. 用 `concurrent.futures.ThreadPoolExecutor(max_workers=1)` + `future.result(timeout=...)` 替代 raw `threading.Thread`
2. 超时后 `future.cancel()` + `executor.shutdown(wait=False)` 不阻塞主流程
3. **关键**：超时后调 `self.unload()` 而非 `self._reset()`，触发 `torch.cuda.empty_cache()` 主动释放显存
4. 添加 `cancel_event = threading.Event()` 作为 best-effort cancel token（`model.generate` 当前不响应，但为未来扩展留接口）

**测试**：L3 `test_generate_raises_timeout_when_exceeding_sla` 验证超时抛 `InferenceTimeoutException` + `backend.is_loaded() == False`，修复后仍通过。

### D2. Blocker B — replayCachedExtracts 幂等短路

**问题**：`TaskOrchestrator.replayCachedExtracts` 不检查 `step.status`，直接 `setStatus(SUCCESS) + save + writeStatement`。若同 taskId 被并发触发两次 replay（极端场景：MQ 重投 + 缓存命中同时发生），会重复写 `financial_statement` 行造成数据膨胀。

**修复**：在 `replayCachedExtracts` 的 `concatMap` 内加 `step.status == SUCCESS` 短路：
- 已 SUCCESS → 跳过 setStatus/save/writeStatement，直接进入下一个 step
- 未 SUCCESS → 走原逻辑（setStatus + save + writeStatement）

**测试**：新增 `shouldShortCircuitReplayForAlreadySuccessfulStep`，验证 BS 已 SUCCESS 时不调 `writeStatement`，IS/CF 仍正常写入。

### D3. Blocker F — reconcileSuccessfulStep 添加 statement 修复钩子

**问题**：进程崩溃发生在 `step.setStatus(SUCCESS) + save` 之后、`statementWriter.writeStatement` 完成之前。MQ 重放 progress SUCCESS 时，`reconcileSuccessfulStep` 进入 EXTRACT_* 分支只调度 CHECK，不重写 statement，导致 CHECK 阶段因数据缺失失败。

**修复**：
1. `StatementWriter` 新增 `ensureStatementWritten(taskId, stepName, fallbackResult)` 方法：
   - 用 `fsRepo.countByReportIdAndStatementType` 检查是否已写入
   - count > 0 → 已存在，返回 count（幂等短路）
   - count == 0 → 调 `writeStatement(taskId, stepName, fallbackResult)` 重写
   - fallbackResult 为 null/empty → 跳过（cache miss 场景，CHECK 自然失败暴露问题）
2. `TaskOrchestrator` 新增 `ensureStatementsWritten(task, taskId)` 私有方法：
   - 从 `reportRepo.findByTaskId` 拿 pdfMd5
   - 对 3 个 EXTRACT step 并行：`extractCacheService.lookup(pdfMd5, step)` → `statementWriter.ensureStatementWritten`
   - report 不存在 / pdfMd5 为空 / cache miss / fsRepo 检查失败均静默跳过
3. 在 `reconcileExtractSuccess` 入口调 `ensureStatementsWritten`，再走原 `checkAllExtractsDone → dispatchStepIfPending(CHECK)` 逻辑

**测试**：新增 `shouldRewriteMissingStatementOnReplayedExtractSuccess`，验证 3 个 EXTRACT step 重放时都调 `ensureStatementWritten`，CHECK 被调度。

## 已完成 checklist

- [x] Blocker A: `llm_loader.py` 改 `concurrent.futures` + 主动 `unload()`
- [x] Blocker B: `TaskOrchestrator.replayCachedExtracts` 加 `step.status == SUCCESS` 短路
- [x] Blocker F: `StatementWriter.ensureStatementWritten` + `TaskOrchestrator.ensureStatementsWritten`
- [x] L2 新增 2 个测试用例（Blocker B + F 各 1 个）
- [x] L2 `mvn clean test` 全绿（**269 tests, 0 failures, 0 errors**，原 267 + 新增 2）
- [x] L3 M2 pytest 全绿（**214 tests, 0 failures**，含 `test_generate_raises_timeout_when_exceeding_sla`）
- [x] 决策记录归档（本文件）
- [ ] squash merge 到 main + push

## 测试基线

| 层 | 命令 | 结果 |
|---|---|---|
| L2 Java 单元测试 | `mvn clean test -Dtest=!ReportParseIntegrationIT` | 269 passed, 0 failures |
| L3 Python M2 测试 | `pytest tests/test_m2_*.py` | 214 passed, 0 failures |
| L1 前端 type-check + lint | 上轮已验证 | 全绿（本轮未触及前端） |

## 未修复风险（剩余 4 项 Major + Minor，作为 M3 follow-up）

- **Major** — `vram_scheduler.py` 的 `evict_idle` LRU 淘汰未持锁，多 worker 并发时可能 evict 正在使用的模型
- **Major** — `modelhub.py` 全局单例在多 worker 进程下不共享，每个 worker 独立加载模型，显存翻倍
- **Major** — `frontend/src/components/StatementTable.vue` 数值输入框无校验
- **Major** — `frontend/src/stores/statements.ts` 的 `edited` Map 未在报告切换时清空
- **Minor** — `ai-service/app/schemas/statement.py` 的 `StatementItem.value: float` 是精度问题根源（需 spec 变更申请）

## 下一步行动项

1. **本轮修复合并**：squash merge `fix/m2-review-blocker-abf` 到 main + push
2. **M3 启动**：3 个 Blocker 已清，可启动 M3 勾稽与异常检测
3. **M3 任务卡补充**：剩余 5 项 Major/Minor 风险归入 M3 modelhub 重构 + 前端补强任务

## 参考

- 修复分支：`fix/m2-review-blocker-abf`
- 上轮决策记录：[docs/decisions/2026-07-21-m2-code-review.md](file:///e:/项目/FinReport%20Agent/docs/decisions/2026-07-21-m2-code-review.md)
- 关联规范：AGENTS.md §8.1 GPU 显存约束 / §8.4 数据一致性 / spec §3.2.1 状态机 / §3.10 抽取缓存 / §10.3 降级链
