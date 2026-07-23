# 决策记录：M3 综合审查与 Bug 修复

日期：2026-07-23
主题：M3.05–M3.09 分支级全面审查、测试验证与 Bug 修复

## 背景

M3.09（前端勾稽页 / 异常页 / 报告页）交付后，M3 阶段任务清单仅剩 M3.10 端到端 SLA 测试。在进入 M3.10 前对当前分支进行一次全面审查，覆盖 L1 前端、L2 Java 后端、L3 Python AI 服务三层代码与测试，目的是把 M3.05–M3.09 在多次会话中累积的潜在 Bug、契约不一致、性能隐患一次性收敛，避免 M3.10 端到端时被底层问题阻塞。

## 决策列表

1. **Major：L2 `ReportArtifactWriter.parseChart` 兼容 `chart_type` 大小写**
   - 现象：L3 `ChartType(str, Enum)` 定义为 `ASSET_STRUCTURE_PIE = "asset_structure_pie"`，`name` 大写、`value` 小写。Pydantic `model_dump()` 默认序列化为 `value`（小写），而 L2 `CHART_TYPE_TO_ARTIFACT_TYPE` 的 key 用大写 enum name。原 `parseChart` 直接 `VALID_CHART_TYPES.contains(chartTypeRaw)` 严格大小写匹配，导致所有真实 L3 图表 payload 进入 L2 时被静默丢弃并 log warn「未知 chart_type」。
   - 修复：入口加 `chartTypeRaw.toUpperCase(Locale.ROOT)` 归一化，匹配后再用大写形式查 `CHART_TYPE_TO_ARTIFACT_TYPE` 映射。同时保留原 raw 值用于日志，便于排查。
   - 选择 L2 入口归一化而非改 L3 `use_enum_values=False`：L3 内部其它模块已依赖 `value` 小写形式做日志与异常消息，改序列化会引发连锁调整；L2 入口兜底还能兼容未来 L9 Agent 编排可能产生的任意大小写输入。

2. **Minor：L2 `ArtifactQueryService.enrichWithUrl` 改 `Mono.fromCallable` 修复调度器失效**
   - 现象：`getPresignedObjectUrl()` 是 MinIO 阻塞 IO。原实现在 `concatMap` lambda 内同步调用，结果包成 `Mono.just(...)`。`subscribeOn(boundedElastic)` 应用在 `Mono.just` 之上时，因 `Mono.just` 在装配期已捕获值，订阅时无工作可调度，阻塞实际落在 `concatMap` 的执行线程（通常是 Reactor 事件循环），违反 spec §12.2「在 SSE 流里同步等待 MQ」同类的性能禁忌。
   - 修复：改用 `Mono.fromCallable(() -> { try { ... } catch (Exception e) { ... } })` 把阻塞调用延后到订阅时执行。FAILED 产物早返回 `Mono.just(toResponse(artifact, ""))` 短路，避免无谓的 MinIO 调用。
   - 选择 `fromCallable` 而非 `Mono.defer`：`fromCallable` 天然包装同步阻塞调用，自动处理 checked exception（MinIO 受检异常在 lambda 内 try/catch 即可），代码更紧凑。
   - 新增 `shouldExecuteMinioCallOnBoundedElastic` 测试用例，用 `thenAnswer` 捕获 `Thread.currentThread().getName()` 并断言含 `boundedElastic`，把性能禁忌作为回归测试永久固化。

3. **不修复的项（已评估为可接受）**
   - `ArtifactQueryService.listArtifacts` 中 `assertReportOwnership(...).thenMany(artifactRepo.findByReportIdOrderByArtifactTypeAsc(reportId).concatMap(...))` 的内层 Flux 在装配期就被组装。生产环境 Spring Data R2DBC 总返回非 null Flux，无 NPE 风险；仅在测试中 Mockito 默认返回 null 时触发 NPE。测试侧通过 `lenient().when(...).thenReturn(Flux.empty())` 解决，未改生产代码，避免引入 `Flux.defer` 增加装配开销。

## 已完成的 Checklist

- [x] `ReportArtifactWriter.java` — `parseChart` 加 `toUpperCase(Locale.ROOT)` + 详细注释
- [x] `ReportArtifactWriterTest.java` — 新增 `shouldAcceptLowercaseChartType` / `shouldAcceptMixedCaseChartType` 2 个用例
- [x] `ArtifactQueryService.java` — `enrichWithUrl` 改 `Mono.fromCallable`，FAILED 产物早返回短路
- [x] `ArtifactQueryServiceTest.java` — 新建文件，8 个用例覆盖 Ownership + EnrichWithUrl + 调度器验证
- [x] L2 全量回归 `338` 个测试全部通过（M3.09 的 328 + 新增 10），BUILD SUCCESS
- [x] L3 全量回归 `495 passed, 1 skipped, 2 failed`（2 个失败是 M1 init_minio/init_milvus 干跑脚本，因测试 venv 未装客户端包，与 M3 改动无关）
- [x] L1 前端 lint + type-check 通过
- [x] `docs/progress/m3.md` 新增「综合审查与 Bug 修复（2026-07-23）」章节

## 发现的风险

- **L3 测试 venv 缺 `minio` / `pymilvus` 客户端包**：导致 `tests/test_m1_init_scripts.py` 中 2 个 `--dry-run` 测试失败。这两个测试是 M1 阶段的脚本烟测，与 M3 改动无关。建议在 M3.10 端到端验证前补齐测试 venv 依赖，或在 `requirements-test.txt` 中显式声明。
- **JaCoCo 离线插桩在 `FlywayMigrationConfiguration` 上误报**：`Index 6 out of bounds for length 0`，需用 `'-Djacoco.skip=true'`（PowerShell 单引号）绕过。这是已知问题（M3.06 / M3.07 决策已记录），不影响测试结果真实性，但覆盖率报告失效。M3.10 验收前需评估是否升级 JaCoCo 版本或切换为 online 插桩。
- **L3 测试 venv 用 Python 3.13**：TRAE 默认 Python 是 3.10.11，`from datetime import UTC` 需 3.11+。当前 `.venv-test` 是临时创建的 3.13 venv，未纳入项目正式依赖管理。建议在 `pyproject.toml` 或 `requirements-test.txt` 中锁定 Python 版本。

## 下一步行动项

- M3.10 端到端 SLA 测试：用真实茅台年报跑完整 PARSE → EXTRACT → CHECK → REPORT 链路，验证 4 分钟内详情页 6 个 Tab 数据完整、PDF 可下载、图表可展示、Markdown 渲染正常。
- M3.10 前补齐 L3 测试 venv 依赖（`minio` / `pymilvus`），让 2 个 M1 init script 测试恢复绿色。
- M3.10 验证 `chart_type` 大小写修复在真实 7B 抽取 + L3 `model_dump()` 序列化 + L2 解析的端到端链路中确实生效（即 3 张图表都能在详情页正常展示）。
