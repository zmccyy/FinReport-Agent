# M2.08 L2 三表并行抽取编排决策记录

> 日期：2026-07-20
> 任务：M2.08 L2 三表并行抽取编排
> 关联：spec §3.2 链路 A / §3.2.1 任务状态机 / plan §4 M2.08
> 进度：[docs/progress/m2.md](../progress/m2.md)

---

## 1. 背景

M2.07 交付了 L3 端的 `Validator` + `extract_with_retry`，但 L2 编排层仍停留在 M1.x 的"串行 mock"：
- `TaskOrchestrator.handleParseSuccess` 直接 transition 到 EXTRACT_RUNNING 但不投递 extract 消息
- 没有"三表都 SUCCESS 才触发 CHECK"的同步点
- 没有"任意 1 条 FAILED → 整任务 FAILED"的失败传播

spec §3.2 链路 A 明确要求三表并行：PARSE_SUCCESS → 同时投递 3 条 extract 消息（BS/IS/CF）→ 等 3 条 progress 都到 → 触发 CHECK。

M2.08 闭环 L2 编排层：交付 `ExtractDispatcher` + `ExtractCompletionTracker` + `TaskOrchestrator` 三处改造，并把 JaCoCo 切换到 offline instrumentation 模式以解决 Windows 非 ASCII 路径下的崩溃。

---

## 2. 决策列表

### D1：Redis AtomicInteger 热路径 + MySQL fallback

**选项**：
- A. 每次 extract progress 都全表扫 `task_step` 判断是否 3 条都 SUCCESS（纯 MySQL）
- B. Redis `INCR fin:extract:done:{taskId}` 原子计数，MySQL 仅作 fallback
- C. L2 内存 `AtomicInteger`（单实例假设）

**决策**：B

**理由**：
- spec §3.2.1 任务状态机要求 3 表都 SUCCESS 才触发 CHECK；每次 progress 都全表扫 `task_step` 是 O(N) 数据库压力
- Redis `INCR` 原子计数 O(1)，热路径毫秒级响应
- Redis 故障时 fallback 到 `checkAllExtractsDone`（MySQL 全表扫描 + `Flux.all()` 短路语义）保证可用性
- L2 内存 `AtomicInteger` 假设单实例，违反 spec §3.5 多 worker 横向扩展要求
- AGENTS.md §8.4 "数据一致性" 允许"失败不强制回滚"，Redis 故障 fallback 默认值（count=0, failed=false）不阻断主流程

### D2：tracker.reset 在 dispatchAll 入口

**背景**：重试场景下，上一轮 extract 部分成功后失败，Redis 残留 done count + failed flag。

**选项**：
- A. dispatchAll 不 reset，依赖 TTL 自然过期
- B. dispatchAll 第一步 `tracker.reset(taskId)` 显式清空

**决策**：B

**理由**：
- TTL=1d 太长，重试时残留计数会污染本轮（如上轮 2 条 SUCCESS 后失败，本轮第 1 条 SUCCESS 时 count=3 误触发 CHECK）
- reset 是幂等操作，多调一次无副作用
- reset 清空 done count + failed flag，保证本轮从干净状态开始

### D3：dispatchAll 用 concatMap 顺序发布

**背景**：spec §3.2 要求"三表并行"，但 L2 发布顺序是否影响下游并行？

**选项**：
- A. `Flux.fromIterable({BS, IS, CF}).flatMap(...)` 并行发布
- B. `Flux.fromIterable({BS, IS, CF}).concatMap(...)` 顺序发布
- C. 串行发布 + 等每条 ack 后再发下一条

**决策**：B

**理由**：
- 下游 3 个 L3 worker 同时消费 `q.extract.requests`，L2 发布顺序不影响下游并行度
- `concatMap` 保证 BS → IS → CF 顺序写入 MQ，便于日志追踪和 debug
- 串行发布违反"三表并行"语义，且增加无意义延迟
- `flatMap` 并行发布在异常路径下日志交错难追踪

### D4：失败 flag 阻断 CHECK

**背景**：3 条 extract 中任意 1 条 FAILED，spec 要求"整任务 FAILED"。但迟到的 sibling SUCCESS 可能仍触发 CHECK。

**选项**：
- A. 只靠 step status 判断（FAILED step 已标记，CHECK 前全表扫发现 FAILED → 跳过）
- B. Redis `fin:extract:failed:{taskId}` flag，`handleExtractStepSuccess` 双查 count + failed

**决策**：B

**理由**：
- 全表扫 `task_step` 找 FAILED step 是 O(N)，每次 progress 都扫开销大
- Redis flag O(1) 查询，`handleExtractStepSuccess` 入口即查，命中 failed flag 直接跳过 CHECK 触发
- 失败 flag 在 `recordFailure` 时 `SET ... EX 86400`，TTL=1d 自动过期，无需手动清理
- 避免迟到的 sibling SUCCESS 触发 CHECK（如 IS FAILED 后，CF 才回 SUCCESS，此时 count 可能=2 + BS=1 + CF=1 = 3 但 IS 已 FAILED）

### D5：terminal FAILED 保留 failed flag

**背景**：重试耗尽转 terminal FAILED 时，是否清除 failed flag？

**选项**：
- A. terminal FAILED 时 `tracker.clearFailure(taskId)` 清空 flag
- B. terminal FAILED 保留 failed flag，TTL=1d 自动过期

**决策**：B

**理由**：
- terminal FAILED 后 MQ 可能仍有 redelivered sibling SUCCESS 消息到达
- 清除 flag 后，redelivered SUCCESS 调 `recordSuccess` → count >= 3 → 误触发 CHECK 进入无意义勾稽
- 保留 flag 阻断所有迟到的 SUCCESS，flag TTL=1d 自动过期不污染下一轮新任务（新任务 dispatchAll 入口 reset）
- spec §8.4 "失败不强制回滚，标记 task.status=FAILED，保留数据供排查" — 保留 flag 符合"保留数据供排查"精神

### D6：reconcile 重放路径绕过 Redis tracker

**背景**：MQ redeliver 场景下，L2 收到重复 SUCCESS 但 `tracker.recordSuccess` 已被上轮调用过，无法重建。

**选项**：
- A. reconcile 路径也调 `tracker.recordSuccess`（可能导致 count 翻倍）
- B. reconcile 路径直接走 MySQL `checkAllExtractsDone`，绕过 Redis tracker

**决策**：B

**理由**：
- Redis `INCR` 是幂等递增，redeliver 会导致 count 翻倍（如 BS SUCCESS 收到 2 次 → count=4 > 3 误触发 CHECK）
- MySQL 是真相源，reconcile 时全表扫判断 3 条都 SUCCESS 更可靠
- reconcile 是冷路径（只在 MQ redeliver 或 Redis 故障时触发），性能开销可接受
- 保持"Redis 热路径 + MySQL 真相源"的清晰边界

### D7：dispatchSingle（EXTRACT_RETRY 路径）clearFailure

**背景**：单步重试（如 BS 失败后重试 BS）时，是否清除 failed flag？

**选项**：
- A. 不清除，依赖新 SUCCESS 覆盖
- B. dispatchSingle 入口 `tracker.clearFailure(taskId)`

**决策**：B

**理由**：
- 上轮 BS 失败设置了 failed flag，本轮 BS 重试若不清除，即使 SUCCESS 也被 flag 阻断 CHECK
- clearFailure 不影响其他 sibling 步骤的 done count（done count 是 `INCR` 累积，clearFailure 只清 failed flag）
- 重试入口清失败状态是直觉正确的语义：本轮重试开始时认为"之前失败已被处理"

### D8：Mockito strict stubbing 配对 stub

**背景**：`ExtractDispatcherTest.shouldMarkTaskAndStepFailedWhenMqPublishFails` 想验证"IS publish 失败 → 整任务 FAILED"。dispatchAll 用 `concatMap` 顺序发布 BS → IS → CF。

**问题**：strict stubbing 模式下，`doThrow(error).when(messageProducer).publishTaskStep(eq(taskId), eq("extract.is"), ...)` 只 stub 了 IS；BS 先被调用，参数 "extract.bs" 不匹配 strict stubbing → PotentialStubbingProblem。

**选项**：
- A. 改用 `lenient()` stub IS
- B. 显式 stub BS 的 `doNothing()`
- C. 改用 `flatMap` 并行发布避免顺序问题

**决策**：B

**理由**：
- `lenient()` 会放过真正的"未 stub 调用"问题，降低测试质量
- 显式 stub BS `doNothing()` 反映 dispatchAll 真实行为：BS 先成功，IS 失败，CF 不再执行
- `flatMap` 并行发布违反 D3 决策（顺序发布便于日志追踪）
- 注释说明"BS doNothing 与 IS doThrow 配对"，未来维护者能理解 strict stubbing 的意图

### D9：TaskOrchestratorTest CF stub 用 lenient

**背景**：`shouldRetainPartialExtractionUntilAllStepsSucceed` 测试 BS SUCCESS + IS MISSING（空 Mono）→ `checkAllExtractsDone` 用 `Flux.all()` 短路语义，predicate 在 IS 处 false → CF 的 stub 未被订阅。

**问题**：Mockito strict stubbing 报 UnnecessaryStubbing。

**选项**：
- A. 改测试场景让 CF 也被订阅（如 BS SUCCESS + IS SUCCESS + CF MISSING）
- B. CF stub 用 `lenient()`

**决策**：B

**理由**：
- 测试意图是"IS MISSING 时 CHECK 不触发"，CF 是否 MISSING 不影响测试断言
- 改场景会偏离测试命名 `shouldRetainPartialExtractionUntilAllStepsSucceed` 的语义
- `lenient()` 只放过 CF 这一个 stub，不影响其他 strict stubbing 检查
- 注释说明"Flux.all 短路导致 CF stub 未订阅"，未来维护者能理解

### D10：JaCoCo 切换到 offline instrumentation 模式

**背景**：本地环境两个非典型路径特征：
1. 工作区路径 `E:\项目\FinReport Agent\backend\` 含非 ASCII 字符 `项目`
2. 用户家目录 `C:\Users\Zm'CC'y'y\.m2\` 含单引号 `'`

surefire 默认 `prepare-agent` 注入 `-javaagent:C:\Users\Zm'CC'y'y\.m2\...jacocoagent.jar` 到 forked JVM。`CommandLineUtils.translateCommandline` 把单引号当引号字符 → "unbalanced quotes" 错误。即使绕过单引号，Boot Manifest-JAR classpath 含 `E:\项目\...` 在 GBK JNU 解码下崩溃 → "other has different root"。

**选项**：
- A. 把 jacoco.dataFile 改到 `C:/Users/Public/`（TRAE sandbox 拒绝写）
- B. 切换到 `jacoco:instrument` offline 模式 + `forkCount=0` 在 Maven JVM 内跑测试
- C. 跳过 JaCoCo（`-Djacoco.skip=true`），放弃覆盖率检查

**决策**：B

**理由**：
- TRAE sandbox 拒绝写 `C:/Users/Public/` 之外的路径，A 不可行
- C 放弃覆盖率门（AGENTS.md §6.1 要求 ≥ 80%），违反规范
- B 完全绕开 `-javaagent` 注入路径：
  - `jacoco:instrument` 在编译时把 probes 字节码插桩到 `target/classes/*.class`
  - `forkCount=0` 测试在 Maven JVM 内跑，不 forked boot JAR
  - `systemPropertyVariables` 传递 `jacoco-agent.destfile` 给 Maven JVM
  - `org.jacoco:org.jacoco.agent:0.8.12:runtime` test 依赖提供 runtime 类
  - `META-INF/jacoco-agent.properties` 配置 destfile / output / dumponexit
- `jacoco:restore-instrumented-classes` 在 verify 阶段恢复原始 .class，避免插桩字节码进入 jar
- 缺点：`forkCount=0` 测试在 Maven JVM 内跑，内存压力大；但当前 211 个测试规模下可接受

---

## 3. 已完成 Checklist

- [x] 实现 `ExtractCompletionTracker.java`（Redis AtomicInteger 热路径 + fallback）
- [x] 实现 `ExtractDispatcher.java`（dispatchAll + dispatchSingle + markDispatchFailed 补偿）
- [x] 改造 `TaskOrchestrator.java`（handleExtractStepSuccess Redis 热路径 + reconcileExtractViaMysql fallback + handleStepFailure 记录 failed flag）
- [x] 编写 `ExtractCompletionTrackerTest.java`（13 个测试）
- [x] 编写 `ExtractDispatcherTest.java`（6 个测试，含 Mockito strict stubbing 配对 stub）
- [x] 改造 `TaskOrchestratorTest.java`（30 个测试全绿，含 lenient CF stub）
- [x] 切换 JaCoCo 到 offline instrumentation 模式（pom.xml + jacoco-agent.properties）
- [x] 跑 `mvn clean verify` BUILD SUCCESS（211 个测试全绿，jacoco:check 通过）
- [x] 更新 `docs/progress/m2.md` 打勾 M2.08

---

## 4. 发现的风险

### R1：L3 端 extract handler 仍是 mock

M2.08 只交付 L2 编排层（dispatch + tracker + state machine），L3 端 `ai-service/app/mq/extract_consumer.py` 仍是 M1.x 的 mock echo。

**缓解**：M2.09 起接入真实 ModelHub 调用链：
- L3 handler 消费 `q.extract.requests` 时 `with VramScheduler.load_for_scene_with_lock(Scene.EXTRACT) as lock: result, validation = extract_with_retry(...)`
- busy 时抛 `ModelLockBusyException` → handler 转 `nack(requeue=True)` + sleep `model_lock_retry_seconds`
- 通过 progress 消息把 `ExtractionResult.statement.to_dict()` + `validation.issues` 回传 L2

### R2：Redis 故障 fallback 性能

Redis 故障时 fallback 到 `checkAllExtractsDone`，每次 progress 全表扫 `task_step`。3 条 progress × O(N) 扫描，N 大时延迟显著。

**缓解**：
- `task_step` 表加 `idx_task_step_status` 复合索引（taskId, stepName, status）
- `Flux.all()` 短路语义：发现 1 条非 SUCCESS 立即返回 false，不全扫
- Redis 故障是低概率事件（M1.x 已配置 Redis Sentinel），fallback 是兜底而非主路径

### R3：terminal FAILED 后 MQ redeliver 可能延迟到达

terminal FAILED 后 1d 内 MQ 可能 redeliver sibling SUCCESS，此时 failed flag 仍有效阻断 CHECK。但 1d 后 flag 过期，redeliver 仍可能误触发 CHECK。

**缓解**：
- MQ 消息 TTL 配置应 < 1d（spec §3.5 默认 24h，建议调到 6h）
- terminal FAILED 任务在 `task` 表标记 `status=FAILED`，即使 CHECK 误触发也会被 `handleCheckStepSuccess` 检查 task.status 阻断下游
- M2.12 集成测试时验证 redeliver 场景

### R4：JaCoCo offline 模式的 forkCount=0 内存压力

`forkCount=0` 所有测试在 Maven JVM 内跑，211 个测试累积内存压力。未来测试规模增长可能 OOM。

**缓解**：
- 当前 211 个测试 + 6GB heap 余量充足
- 未来若 OOM，可改 `forkCount=1` + `reuseForks=false`（每个测试类 fork 新 JVM），但要重新解决 `-javaagent` 路径问题
- 备选方案：用 `argLine` 显式传 `-javaagent` 时用 `file:///` URL 编码避开单引号（`'` → `%27`）和 GBK 路径

### R5：Mockito strict stubbing 配对 stub 易遗漏

D8 决策要求"IS doThrow 配对 BS doNothing"，未来维护者新增"CF doThrow" 测试时可能漏配对 BS/IS stub。

**缓解**：
- `ExtractDispatcherTest` 注释明确说明 strict stubbing 配对规则
- 测试 `shouldMarkTaskAndStepFailedWhenMqPublishFails` 命名清晰，未来维护者改 dispatchAll 时会先看此测试
- 必要时抽 `stubAllThreePublishes(mockProducer, taskId, bsBehavior, isBehavior, cfBehavior)` 辅助函数

---

## 5. 下一步行动项

1. **M2.09 L2 抽取结果写 financial_statement**：
   - L3 extract handler 调 `extract_with_retry` 拿到 `ExtractionResult.statement.to_dict()` + `validation.issues`
   - 通过 progress 消息回传 L2（payload 含 statement JSON + warnings）
   - L2 `StatementWriter` 消费 progress 把 JSON 写入 MySQL `financial_statement` 表（spec §5.2 表结构）
   - 加 `validation_warnings` JSON 字段存 `validation.issues`（缓解 M2.07 R1）
   - 验收：单份年报三表共 ~150 条科目记录入库；confidence 字段写入

2. **M2.10 抽取结果缓存**：
   - 同 pdf_md5 重传直接命中 Redis 缓存
   - `ExtractCacheService` 在 dispatchAll 入口查缓存，命中跳过 extract 直接 CHECK
   - 验收：重传同 PDF → 跳过 extract 步骤 → 直接 CHECK

3. **M2.12 集成测试**：
   - 验证 Redis 故障 fallback 性能（缓解 R2）
   - 验证 terminal FAILED 后 MQ redeliver 场景（缓解 R3）
   - 端到端耗时（PARSE+EXTRACT） < 3 min（spec §12.1 SLA）
