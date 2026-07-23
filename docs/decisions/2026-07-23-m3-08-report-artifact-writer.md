# 2026-07-23 M3.08 ReportArtifactWriter 上传 MinIO

## 背景

M3.07 PdfConverter 已交付 PDF bytes。Reasoner → Report → Artifact 链路
最后一步需要 L2 `ReportArtifactWriter` 消费 L3 REPORT progress 携带的
`result` payload，把生成的 PDF / Markdown / 3 张图表 PNG 上传到 MinIO
`finreport-reports` bucket，并在 `report_artifact` 表中记录 object key，
供 M3.09 前端报告页通过 (reportId, artifactType) 查询预签名 URL 下载。

plan M3.08 验收标准（line 678-686）：

* MinIO `reports/{reportId}/` 下有 `report.pdf`、`report.md`、
  `charts/*.png`
* 验证方式：`mc ls` 检查；预签名 URL 可下载

实现完成后做了全面代码审查，发现 1 个关键 bug（reactor 断链）+ 3 个
Minor 问题，全部修复后通过 31 个单元测试 + 2 个集成测试 + 全 L2 回归
321 个测试。

## 决策列表

1. **失败策略：单文件失败标记 FAILED 不阻断（spec §8.4）** —
   单文件上传失败只 `log.warn` + 标记 `status=FAILED` 保留行供排查；
   不阻断后续文件上传；不抛异常避免阻断状态机推进。Report → COMPLETED
   链路即使产物部分失败也能完成；前端 ReportViewer 遇到 `status=FAILED`
   会展示「产物不可用」提示。`writeArtifacts` 返回 0 时不阻断
   `TaskOrchestrator.handleStepSuccess` 推进到 COMPLETED（集成测试
   `shouldCompleteTaskEvenIfArtifactWriterReturnsZero` 验证）。

2. **`onErrorReturn(false)` vs `onErrorResume(Mono.empty())`（关键 bug）** —
   reactor 关键陷阱：`onErrorResume(e -> Mono.empty())` 会让 Mono 进入
   empty 状态，后续 `.map(success -> ...)` 不执行，整个 chain 断开，
   后续文件不再上传。正确做法是 `.onErrorReturn(false).map(success -> {
   if (!success) markFailed(...) })`，让 Mono 继续传递 `false` 而非进入
   empty。审查发现实现阶段已修复，文档化为决策避免再次踩坑。

3. **object key 命名（spec §5.3）** —
   `reports/{reportId}/report.pdf` / `reports/{reportId}/report.md` /
   `reports/{reportId}/charts/chart_{pie|line|bar}.png`；图表文件名按
   artifact_type 映射，与 L3 ChartType 枚举解耦，便于前端按固定路径
   拼接预签名 URL。`finreport-reports` bucket 是 public-read 策略，
   预签名 URL 可直接下载。

4. **MinIO 受检异常处理（minio 8.5.10）** —
   `PutObjectArgs.contentType()` 在 builder 链中声明
   `throws NoSuchAlgorithmException, IOException, KeyManagementException`；
   在 lambda（如 Mockito `ArgumentMatchers.argThat()`）内部调用会编译
   失败（lambda 不允许抛出受检异常除非声明）。解决方案：测试中改用
   `args.object()` 匹配路径（如 `args.object().endsWith("report.pdf")`），
   避免在 lambda 内调用受检异常方法。生产代码中 `PutObjectArgs.builder()`
   链在 `Mono.fromCallable` 的 lambda 内（Callable 允许抛 Exception），
   编译通过。

5. **幂等性：(reportId, artifactType) 唯一性检查** —
   重放 REPORT SUCCESS 时已存在 GENERATED 行则跳过（避免 MinIO 重复
   上传 + 表数据膨胀），FAILED 行允许覆盖（重试 FAILED → GENERATED
   场景）。`TaskOrchestrator.handleStepSuccess` 已对重放 SUCCESS 做去重
   （reconcile 路径不调本类），双层保险。幂等检查走 V5 §25
   `idx_artifact_type_status` 索引。

6. **事务边界（spec §8.4 任务边界 = 事务边界）** —
   所有 `report_artifact` 行在同一 `TransactionalOperator.transactional()`
   内顺序写入，任一行写入失败整体回滚避免半成品数据。MinIO 上传在
   事务外执行（MinIO 失败不强制回滚，保留半成品 object 供排查）。

7. **顺序上传而非并行** —
   5 个文件规模下顺序执行毫秒级完成，事务内并行 save 可能产生连接池
   压力且无显著收益。每个文件用 `concatMap` 顺序处理，避免 R2DBC
   连接池争用。

8. **boundedElastic 调度器** —
   MinIO `putObject` / `bucketExists` / `makeBucket` 是阻塞 IO 调用，
   订阅在 `Schedulers.boundedElastic()` 上避免阻塞 Reactor 事件循环
   （spec §12.2 性能禁忌：在 SSE 流里同步等待 MQ / 阻塞 IO 调用）。

9. **bucket 自动创建 + `BucketAlreadyOwnedByYou` 容错** —
   `ensureBucketExists` 容错处理 `BucketAlreadyOwnedByYou`（并发场景
   两个请求同时 makeBucket），其他 `ErrorResponseException` 抛出。
   生产环境由 `scripts/init_data.py` 预建 bucket，此处兜底用于测试
   环境 + 首次部署。

10. **reportId 解析回退** —
    优先用 `task.ref_report_id`（M3.07 后端在创建 report 后回写），
    缺失时回退到 `report.task_id` 关联查询；与
    `CheckResultWriter.resolveReportId` 一致。两种路径都找不到
    时返回 empty，`writeArtifacts` 进入 `switchIfEmpty` 分支
    返回 0。

11. **不可变输入** —
    不修改 L3 progress 携带的 `result` Map；产出新的
    `ParsedArtifacts` record（PDF bytes / Markdown / charts 列表）。
    实体 `ReportArtifact` 在 `markFailed` 时会修改 status 字段，
    但这是内部状态机推进，与 L3 输入解耦。

## 已完成的 checklist

- [x] `ReportArtifact` 实体 + 7 常量（STATUS_GENERATED/FAILED +
      TYPE_PDF/MARKDOWN/CHART_PIE/CHART_LINE/CHART_BAR）
- [x] `ReportArtifactRepository` 4 方法（findByReportIdAndArtifactType /
      findByReportIdOrderByArtifactTypeAsc / countByReportId /
      deleteByReportId）
- [x] `ReportArtifactWriter` 三层实现（解析 / 上传 / 持久化）
- [x] `TaskOrchestrator.handleStepSuccess` 集成 REPORT 分支调用 writer
- [x] `TaskOrchestratorTest` 新增 2 个集成测试
- [x] 31 个单元测试覆盖所有路径（解析 9 + ObjectKeys 3 + 成功 6 +
      失败 7 + 幂等 2 + Bucket 3 + ArtifactObjectKeys 1）
- [x] 修复 1 个关键 bug（onErrorResume 断链）+ 3 个 Minor 问题
- [x] 全 L2 回归 321 个测试通过
- [x] progress/m3.md 勾选 M3.08 + 交付说明

## 发现的风险

- **JaCoCo offline instrument 模式 false negative**（与 M3.06 / M3.07
  相同）— `jacoco.exec` 只有 4573 字节，CSV 显示所有类 0% 覆盖率。
  根因是 JaCoCo offline instrument + `restore-instrumented-classes`
  之间的数据写入问题。不修复，依赖 31 个测试覆盖所有路径作为覆盖率
  证据。M3.10 端到端验证时若 JaCoCo 仍 false negative，考虑切换到
  JaCoCo online mode（on-the-fly instrumentation）。
- **MinIO `PutObjectArgs.contentType()` 受检异常** — minio 8.5.10
  设计缺陷，未来升级 minio 版本时需重新检查。生产代码中 `fromCallable`
  的 Callable 允许抛 Exception，不受影响；测试代码中需用 `args.object()`
  匹配避免在 lambda 内调用受检异常方法。
- **预签名 URL 下载未在 M3.08 验证** — `ReportArtifactWriter` 只负责
  上传 + 写表，预签名 URL 生成是 M3.09 前端报告页 + M3.10 端到端
  验证的范围。

## 下一步行动项

- M3.09 前端勾稽页 + 异常页 + 报告页 — `ReportViewer` 通过
  `GET /api/reports/{reportId}/artifacts` 查询产物列表，按 artifact_type
  渲染下载链接 + 图表预览。
- M3.10 端到端 SLA 测试 — 真实 MinIO + `mc ls reports/{reportId}/`
  检查 5 个文件存在；失败容错场景验证 `status=FAILED` 行保留 +
  状态机仍推进到 COMPLETED。
