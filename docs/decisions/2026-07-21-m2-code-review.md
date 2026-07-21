# 2026-07-21 M2 代码审查与 P0+P1 修复

## 背景

M2 里程碑（M2.00–M2.12，commit `7a60516`）squash 合并到 main 后，对仓库执行全面 code-review，识别出 6 个 Blocker、14 个 Major、11 个 Minor 问题。本轮优先修复 P0（数据丢失/状态机不变量破坏）与 P1（安全/精度/NPE/排序/校验）共 8 项；剩余 P2（vram_scheduler 并发、modelhub 单例、前端输入校验、内存泄漏等）作为 M3 启动前的 follow-up。

审查范围：M2 涉及的 17 个核心文件（L2 Java 8 + L3 Python 4 + L1 Vue 2 + 测试 3）。

## 决策列表

### D1. TaskOrchestrator.scheduleRetryOrFail — clearFailure 时序修正（Blocker C）

**问题**：原实现先 `extractTracker.clearFailure(taskId)` 再 `messageProducer.publishRetry(...)`。若 publishRetry 失败（MQ 不可用），tracker 已清空 failure 计数，但消息未投出，导致重试链路断裂、任务卡死。

**修复**：调整为 `save → publishRetry → clearFailure`，clearFailure 移到 publishRetry 之后；同时把 `onErrorMap` 改为 `onErrorResume`，捕获 `IntegrationException` 后调用 `markDispatchFailed` 标记 step 失败，再向下游传播错误。

**测试**：`TaskOrchestratorTest$ReliabilityAndOwnershipBranches` 9 个用例全绿。

### D2. StatementWriter.toBigDecimal — 精度损失修正（Blocker D）

**问题**：`BigDecimal.valueOf(double)` 在 double → BigDecimal 时存在二进制浮点表示误差（如 `1.23e9` 实际存储为 `1229999999.9999999...`），写入 MySQL DECIMAL 字段后精度失真。L3 schema 中 `StatementItem.value: float` 是精度问题根源，但改 schema 影响面大（涉及 Pydantic 模型 + JSON Schema + 前端类型），留作 M3 follow-up。

**修复**：L2 写入侧用 `new BigDecimal(n.toString())` 中转 String 兜底，保证 Number（含 Integer/Long/Double/BigDecimal）→ BigDecimal 的无损转换；NumberFormatException 时返回 null（让 StatementWriter 跳过该行，不污染 DB）。

**测试**：`StatementWriterTest$WriteStatement` 20 个用例全绿。

### D3. TaskStateMachine — 允许 *_RUNNING → FAILED（Blocker E 配套）

**问题**：`markDispatchFailed` 在 publishRetry 失败时把 task 置为 `FAILED`，但 `TaskStateMachine.ALLOWED` 中 `PARSE_RUNNING / EXTRACT_RUNNING / EXTRACT_PARTIAL / CHECK_RUNNING / REPORT_RUNNING` 的合法后继集合不含 `FAILED`，导致状态机校验抛异常，markDispatchFailed 自身失败。

**修复**：给上述 5 个 *_RUNNING 状态的 ALLOWED 集合追加 `FAILED` 与 `CANCELLED`（保留原集合）。这与 spec §3.2.1「任何 RUNNING 状态都可因不可恢复错误转 FAILED」语义一致。

**测试**：`TaskStateMachineTest` 全部 39 个用例（10 个子套件）全绿。

### D4. StatementQueryService — NPE 防护 + 排序稳定性（Major）

**问题 1**：原 `report.getUserId().equals(userId)` 在 `report.getUserId() == null`（理论不应发生，但防御性编程要求）时抛 NPE，整个查询失败。
**修复 1**：改为 `userId.equals(report.getUserId())`，userId 由 JWT 解析保证非 null。

**问题 2**：`findByReportIdAndStatementType` 无 ORDER BY，SQL 返回顺序未定义，前端三表展示顺序随机。
**修复 2**：在 `FinancialStatementRepository` 新增 `findByReportIdAndStatementTypeOrderByItemNameAsc`，三表查询改用该方法，保证按科目名字典序稳定排序。原方法保留（`ReportParseIntegrationIT` 仍用）。

**测试**：`StatementQueryServiceTest$GetStatements` 4 个用例 + `GetReportDetail` 3 个用例全绿。

### D5. JwtFilter — Redis 故障拒绝而非放行（Major，安全）

**问题**：原 `JwtFilter` 在 Redis 黑名单查询失败时 `onErrorResume` 返回 `chain.filter(exchange)` 放行请求。这违反 spec §8.5「JWT 登出加黑名单」的安全语义——Redis 故障期间，已登出的 token 仍可访问受保护资源。

**修复**：Redis 故障时返回 503 Service Unavailable，提示「认证服务暂不可用，请稍后重试」。新增 `respond503` 私有方法构造 503 响应体（JSON Problem Details 格式，符合 RFC 9457）。

**测试**：`SecurityComponentsTest$JwtFilterTests` 9 个用例全绿；其中 `shouldRejectWith503WhenBlacklistLookupFails`（原 `shouldDegradeOpenIfBlacklistLookupFails`）断言更新为期望 503。

### D6. Extractor handler — 未知 step 显式报错（Major）

**问题**：L3 `extractor/handler.py` 对未知 `message.step`（非 `extract.bs/is/cf`）静默 fallback 到 `balance_sheet` 并返回 `success=True`。若 MQ 路由配置错误把 `extract.xyz` 投进来，会写入 balance_sheet 假数据且无告警，污染知识库。

**修复**：未知 step 抛 `ValueError`，让 MQ consumer 走 DLQ，避免污染数据。错误消息列出合法 step 集合，便于排查。

**测试**：L3 pytest 待用户在 docker 容器内执行（`py_compile` 语法已验证）。

### D7. Extractor validator — assert → if 防御性检查（Major）

**问题**：`validator.py` 用 `assert result.statement is not None` 校验 success=True 时 statement 非空。Python `-O` 启动时 assert 被剥离，理论不应发生的 None 会绕过校验进入 `validate_statement` 触发 NPE。

**修复**：改为显式 `if result.statement is None: return ValidationResult(is_valid=False, ...)`，返回明确的 `missing_statement` error code，error_hint 提示「statement payload missing despite success flag」。

**测试**：L3 pytest 待用户在 docker 容器内执行（`py_compile` 语法已验证）。

## 已完成 checklist

- [x] L2 Java 8 文件修复（TaskOrchestrator / StatementWriter / TaskStateMachine / StatementQueryService / FinancialStatementRepository / JwtFilter）
- [x] L3 Python 2 文件修复（handler.py / validator.py）
- [x] L2 测试同步更新（SecurityComponentsTest / StatementQueryServiceTest）
- [x] `mvn clean test` 全绿（267 tests, 0 failures, 0 errors）
- [x] L3 语法编译验证（`py_compile` 通过）
- [x] 决策记录归档（本文件）
- [ ] L3 pytest 完整验证（待用户在 docker 容器内执行）
- [ ] 合并到 main（待用户决定是否延续上次豁免）

## 发现的风险（未在本轮修复，作为 M3 follow-up）

### Blocker A — llm_loader 超时线程未停止
`ai-service/app/modules/modelhub/llm_loader.py` 用 daemon 线程做超时控制，但线程内调用的 `load_model` 阻塞时无法被中断，超时后主流程返回但加载线程仍在跑，可能持续占用 GPU 显存。M3 modelhub 重构时改为 `concurrent.futures` + cancel 机制。

### Blocker B — replayCachedExtracts 不检查 step 状态
`TaskOrchestrator.replayCachedExtracts` 直接写库，不检查 step 是否已是 SUCCESS。若同 taskId 被并发触发（极端场景），会重复写 financial_statement 行。M3 在 replay 前加 `step.status == SUCCESS` 短路。

### Blocker F — reconcileSuccessfulStep 不重写 statement
`TaskOrchestrator.reconcileSuccessfulStep` 在 step.status 已是 SUCCESS 但 statement 未写入的场景下，不重写 statement，导致数据丢失。M3 添加「step SUCCESS 但 statement 缺失」的修复钩子。

### Major — vram_scheduler 并发
`vram_scheduler.py` 的 `evict_idle` LRU 淘汰未持锁，多 worker 并发时可能 evict 正在使用的模型。M3 加 `model_lock` 保护。

### Major — modelhub 单例
`modelhub.py` 全局单例在多 worker 进程下不共享，每个 worker 独立加载模型，显存翻倍。M3 改为 Redis 分布式锁 + 单 worker 加载 + 其他 worker 远程调用。

### Major — StatementTable 输入无校验
`frontend/src/components/StatementTable.vue` 的数值输入框无校验，用户可输入非数字字符串。M3 前端补 `el-input-number` + 范围校验。

### Major — statements.ts 内存泄漏
`frontend/src/stores/statements.ts` 的 `edited` Map 未在报告切换时清空，长期使用会累积所有查看过的报告的编辑状态。M3 加 `resetEdited` action 在路由切换时调用。

### Minor — L3 schema value: float 精度
`ai-service/app/schemas/statement.py` 的 `StatementItem.value: float` 是 Blocker D 的根源。M3 改为 `value: str`（序列化时用字符串），L2/前端解析为 BigDecimal/number。影响面大，需 spec 变更申请。

## 下一步行动项

1. **本轮修复合并**：`fix/m2-review-p0-p1` 分支已就绪，用户决定是否延续上次豁免直接合并 main，或开 PR 走 review。
2. **L3 pytest 验证**：用户在 docker dev 栈内执行 `docker compose exec ai-service pytest tests/test_m2_extractor.py tests/test_m2_validator.py -v`。
3. **M3 启动准备**：上述 7 个未修复风险中，Blocker A/B/F 必须在 M3 modelhub 重构前修复；Major 4 项可并行处理。
4. **M3 任务卡复盘**：基于本轮审查发现，在 `docs/superpowers/plans/2026-07-13-finreport-agent-implementation-plan.md` 的 M3 任务卡中补充「modelhub 单例改造」「vram_scheduler 加锁」「schema value 改 str」三个新任务。

## 参考

- 审查范围：M2 squash commit `7a60516`
- 修复分支：`fix/m2-review-p0-p1`
- 测试基线：L2 267 tests passed / L3 语法验证通过（pytest 待用户执行）
- 关联规范：AGENTS.md §8 关键技术约束 / §9 AI 协作规范 / §10 错误处理规范 / spec §3.2.1 状态机 / §8.5 安全 / §10.3 降级链
