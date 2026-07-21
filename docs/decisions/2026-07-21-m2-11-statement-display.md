# M2.11 前端三表展示页决策记录

> 日期：2026-07-21
> 任务：M2.11 前端三表展示页
> 关联：spec §6.5.1 `/reports/:id` 页面结构 / §5.2.2 `financial_statement` 表 / plan §4 M2.11
> 进度：[docs/progress/m2.md](../progress/m2.md)

---

## 1. 背景

M2.10 已交付抽取结果缓存（`fin:cache:extract:{pdfMd5}:{step}` + 7d TTL），M2.09 已交付 `financial_statement` 表写入。前端 M1.16 已交付上传 / 列表 / 进度三页，但缺详情页：用户无法查看解析后的三表数据。

spec §6.5.1 要求：`/reports/:id` 路由以 Tab 结构展示资产负债表 / 利润表 / 现金流量表；plan M2.11 验收标准为"三表分 Tab 展示；科目名、数值、单位、期间、scope 字段正确显示；可手动编辑数值（暂不写回）"。

M2.11 闭环前端三表展示：
- 后端：新增 `GET /api/v1/reports/{reportId}` + `GET /api/v1/reports/{reportId}/statements` 两个端点，封装到 `StatementQueryService`，含用户归属校验
- 前端：新增 `ReportDetail.vue`（Tab 结构）+ `StatementTable.vue`（可编辑表格组件）+ `stores/statements.ts`（Pinia store）+ `api/statements.ts`（API 封装）+ 路由 + 列表页/进度页跳转入口

---

## 2. 决策列表

### D1：后端单端点聚合 vs 三端点独立查询

**背景**：`/reports/{reportId}/statements` 是返回三表合一的聚合响应，还是分三次请求？

**选项**：
- A. 三次独立查询：`/statements/{reportId}/balance-sheet`、`/income-statement`、`/cash-flow` — 前端按 Tab 懒加载
- B. 单端点聚合：`/statements/{reportId}` 一次返回 `{balanceSheet, incomeStatement, cashFlow}`

**决策**：B

**理由**：
- 前端 Tab 切换零延迟（已预加载三表数据），用户体验更流畅
- 单次 HTTP RTT 节省网络往返（即便 HTTP/2 多路复用，三次请求仍需三次业务查询 + 三次归属校验）
- 后端 `findByReportIdAndStatementType` 已是按类型分组的 R2DBC 查询，串行 3 次内网查询 < 30ms
- A 股三表合计科目数 ~150 行，单次响应 payload ~30KB JSON，无带宽压力
- 风险：若未来单表条目激增（如季度明细），可加 `?type=balance_sheet` 过滤参数；当前不在 spec 范围内

### D2：用户归属校验放在 Service 层

**背景**：`ReportController` 应该校验 `report.userId == X-User-Id` 还是 `StatementQueryService` 校验？

**选项**：
- A. Controller 层校验：在 endpoint 方法里先 `reportRepo.findById` 校验
- B. Service 层校验：Service 内部统一封装"查询 + 校验 + 转换"

**决策**：B

**理由**：
- AGENTS.md §3.1 明文禁止"在 Controller 写业务逻辑"
- `StatementQueryService.getReportDetail` 和 `getStatements` 共享同一段归属校验逻辑（`reportRepo.findById(reportId).filter(r -> r.getUserId().equals(userId)).switchIfEmpty(error)`），下沉到 Service 避免重复
- 未来若 `ReportController` 新增 `/reports/{reportId}/checks` 等端点，可复用同一 Service 校验路径
- Controller 仅做 HTTP 协议层职责（路径变量解析 / Header 透传 / 响应包装），不耦合业务

### D3：`StatementsResponse` 用 `List.copyOf` 防御性复制

**背景**：Java record 的 List 字段是否需要防御性复制？

**决策**：compact constructor 中用 `List.copyOf(balanceSheet)` 等包裹

**理由**：
- 调用方可能传入 `ArrayList` 等可变 List，record 不变性仅在引用层面（final 字段），List 内容仍可被外部修改
- `List.copyOf` 返回不可修改的 List（`Collections.unmodifiableList` 包装的不可变副本），保证 DTO 内部状态不可变
- 防御性复制 + SpotBugs `EI_EXPOSE_REP` 排除（`spotbugs-exclude.xml`）双保险：静态分析无法证明 `List.copyOf` 不可变性，显式排除避免 CI 误报
- 极端情况：调用方传入 `null`，compact constructor 兜底转 `List.of()`，避免 NPE 在序列化阶段才暴露

### D4：编辑值本地存储（不写回后端）

**背景**：plan M2.11 明确"可手动编辑数值（暂不写回）"——如何实现？

**决策**：编辑值存 Pinia store 的 `edited.values: Record<string, string>`，key 为 `${statementType}:${itemId}`

**理由**：
- spec §6.5.1 描述的"暂不写回"对应 M3 阶段的勾稽修正——M3 完成异常检测后才会决定"哪些值需要回写 DB"
- 当前编辑仅用于"预览"和"待评估的修正建议"，无需持久化
- `Record<string, string>` key 设计 O(1) 查询；`displayValue(item)` 优先返回编辑值，未编辑时返回 backend `itemValue`
- 编辑过的单元格视觉高亮（橙色边框 + 浅黄背景），头部展示"已编辑 N 项"计数
- 风险：刷新页面编辑丢失。已在 `ReportDetail.vue` 底部 footer 提示"编辑后请保存截图或导出，刷新页面将丢失"，避免用户误以为已保存

### D5：`activeTab` 用字符串联合类型而非 enum

**背景**：TypeScript 中 `activeTab` 类型如何定义？

**选项**：
- A. `enum Tab { BalanceSheet, IncomeStatement, CashFlow }` — 编译期常量
- B. `type Tab = 'balance_sheet' | 'income_statement' | 'cash_flow'` — 字符串字面量联合

**决策**：B

**理由**：
- 后端 `statement_type` 字段是 String 常量（`balance_sheet` / `income_statement` / `cash_flow`），与 L3 `StatementType.value` 对齐（spec §5.2.2）
- 字符串联合类型在 axios 响应中可直接对齐，无需 enum↔string 转换；减少类型不匹配的边界
- Vue + TypeScript 工具链对字符串联合推断更友好（IDE 自动补全 / 模板字面量类型推断）
- 与 `types/index.ts` 中 `StatementType` 类型保持一致（也是字符串字面量联合）

### D6：Playwright E2E 推迟到 M2.12

**背景**：M2.11 是否要写 Playwright E2E 测试？

**决策**：M2.11 不引入 Playwright，E2E 推迟到 M2.12

**理由**：
- AGENTS.md §9.2 禁止"引入设计文档未提及的新依赖"——当前项目无 Playwright 配置（`package.json` 中无 `@playwright/test` 依赖）
- spec §6.5.1 未明确要求 M2.11 E2E；plan M2.11 验收标准为"上传年报 → 详情页查看三表"，这是 M2.12 集成测试的范围
- M2.11 通过手工浏览器验证 + 后端 13 个单元测试（6 controller + 7 service）覆盖核心路径
- M2.12 集成测试会引入 Testcontainers + Playwright，整体发版前统一处理 E2E
- 风险：M2.11 至 M2.12 之间前端回归靠人工；可接受（M2.11 与 M2.12 时间间隔短，无第三方依赖变更）

### D7：`ReportController` 保留单参数构造器兼容旧测试

**背景**：新增 `StatementQueryService` 依赖后，旧 `ControllerUnitTest` 是否需要重写？

**选项**：
- A. 删除旧单参数构造器 `ReportController(FileService)`，强制更新所有测试
- B. 保留单参数构造器 + 新增双参数构造器，旧测试不破坏

**决策**：B

**理由**：
- Spring 4.3+ 推荐 constructor injection，但保留单参数构造器让旧测试代码无感知
- 旧测试 `ControllerUnitTest` 仅 mock `FileService`，新增 `@Mock private StatementQueryService statementQueryService` 即可
- Spring DI 优先使用参数最多的构造器（双参数构造器），生产代码无影响
- 风险：单参数构造器未来若被废弃可删除，但 M2.11 保守起见保留

### D8：`load(reportId)` 并发请求 + 统一错误处理

**背景**：`stores/statements.ts` 的 `load` action 如何编排 `getReportDetail` 和 `getStatements` 两个 API？

**决策**：`Promise.all([getReportDetail(reportId), getStatements(reportId)])` 并发 + `catch` 统一错误处理

**理由**：
- 两个请求相互独立，无依赖关系，并发执行节省 1 RTT
- 任意一个失败 → `catch` 设置 `store.error`，UI 展示错误态 + 重试按钮
- 错误信息透传：backend 返回 RFC 9457 格式 `{type, title, status, detail, traceId}`，前端 axios 拦截器统一提取 `detail` 字段
- 风险：若 backend 部分端点故障（如 `/reports/{id}` 200 但 `/reports/{id}/statements` 500），用户无法查看部分数据；可接受（用户优先看到错误，避免数据不一致体验）

### D9：`displayValue(item)` 优先返回编辑值

**背景**：表格中显示的值如何选择 backend 值还是本地编辑值？

**决策**：`edited.values[\`${statementType}:${itemId}\`] ?? item.itemValue`

**理由**：
- 用户编辑后的值应立即反映在 UI（无需刷新）
- 未编辑的单元格显示 backend `itemValue`（String 类型，BigDecimal 序列化为字符串）
- `el-input :model-value="displayValue(row)"` 受控模式，`@update:model-value` 触发 `editValue` 写入 store
- 编辑过的单元格 CSS 类 `value-input--edited`（橙色边框 + 浅黄背景），视觉反馈明确
- 风险：若 backend `itemValue` 为 `null`（极少数情况，spec §5.2.2 允许 NULL），显示空字符串；用户编辑后正常显示

---

## 3. 已完成 checklist

### 后端

- [x] `StatementItemResponse` record：id / statementType / itemName / itemValue / currency / unit / scope / periodType / confidence / sourcePage
- [x] `StatementsResponse` record：balanceSheet / incomeStatement / cashFlow + `List.copyOf` 防御性复制 + `empty()` 静态工厂
- [x] `ReportDetailResponse` record：id / taskId / companyCode / companyName / reportType / reportPeriod / pageCount / parseStatus / pdfObjectKey / createdAt
- [x] `StatementQueryService`：
  - [x] `getReportDetail(reportId, userId)` 含归属校验
  - [x] `getStatements(reportId, userId)` 含归属校验 + 三表分组
  - [x] 业务异常用 `BusinessException(NOT_FOUND, "REPORT_NOT_FOUND", ...)` 而非 RuntimeException
- [x] `ReportController` 新增端点：
  - [x] `GET /api/v1/reports/{reportId}` → `Mono<ResponseEntity<ReportDetailResponse>>`
  - [x] `GET /api/v1/reports/{reportId}/statements` → `Mono<ResponseEntity<StatementsResponse>>`
  - [x] 双参数构造器 `(FileService, StatementQueryService)` + 保留单参数构造器兼容旧测试
- [x] `ControllerUnitTest` 适配新依赖（`@Mock StatementQueryService`）
- [x] `spotbugs-exclude.xml` 排除 `StatementsResponse` 的 `EI_EXPOSE_REP` 误报
- [x] 测试：`ReportControllerStatementsTest` 6 个 + `StatementQueryServiceTest` 7 个 = 13 个新测试
- [x] 全量测试：`mvn clean verify` BUILD SUCCESS，263 个测试全绿
- [x] 覆盖率：`jacoco:check` 通过（≥80% 门槛）
- [x] 质量门：`mvn checkstyle:check spotbugs:check` 通过

### 前端

- [x] `types/index.ts` 新增类型：`StatementType` / `StatementItem` / `StatementsResponse` / `ReportDetail` + `STATEMENT_TYPE` 常量
- [x] `api/statements.ts`：`getReportDetail(reportId)` + `getStatements(reportId)`
- [x] `stores/statements.ts`：state（report / statements / loading / error / edited / activeTab） + actions（load / setTab / editValue / displayValue / resetEdits / reset）
- [x] `components/StatementTable.vue`：可编辑表格 + 编辑高亮 + 置信度 Tag + 来源页 + 空态
- [x] `views/ReportDetail.vue`：公司头部 + 状态 Tag + 元信息卡片 + 加载态 + 错误态 + 重试 + Tab 三表 + 编辑提示 footer
- [x] `router/index.ts`：新增 `/reports/:reportId` 路由（`requiresAuth: true`）
- [x] `views/Reports.vue`：操作列新增"查看三表"按钮（`reportId == null` 时禁用）
- [x] `views/TaskProgress.vue`：完成态新增"查看三表"按钮（`taskStatus === 'COMPLETED' && reportId != null` 时启用）
- [x] 质量门：`npm run lint` / `npm run type-check` / `npm run build` 全绿

### 文档

- [x] `docs/progress/m2.md`：M2.11 打勾 + 交付说明
- [x] 决策归档：本文件

---

## 4. 发现的风险

### R1：编辑值刷新丢失

编辑值仅存 Pinia store（内存），刷新页面或路由切换后丢失。M2.11 显式"暂不写回"是 plan 约定，但用户可能误以为已保存。

**缓解**：`ReportDetail.vue` 底部 footer 显式提示"编辑后请保存截图或导出，刷新页面将丢失"。

### R2：未做权限细分

当前仅校验 `report.userId == X-User-Id`，未做角色权限（如 admin 查看所有用户报告）。spec §6.5.1 未要求角色权限，M3/M4 若需 admin 后台需新增 `/internal/reports/{id}` 端点（绕过 userId 校验）。

### R3：三表全量加载

`/statements` 端点一次返回三表全部科目（~150 行）。A 股单家财报三表条目数稳定，但若未来支持季度明细或多年度对比，需考虑分页或懒加载。

### R4：Playwright E2E 推迟

M2.11 至 M2.12 之间前端回归靠人工 + 单元测试。M2.12 引入 Playwright 后需补关键路径 E2E（上传 → 解析完成 → 查看三表 → 编辑 → 切换 Tab）。

### R5：BigDecimal 序列化为字符串

后端 `itemValue` 是 `BigDecimal`，Jackson 默认序列化为字符串（避免 double 精度丢失）。前端展示时若需做数值计算（如求和），需 `parseFloat`。当前 `displayValue` 返回字符串原样展示，无计算需求。

### R6：跨标签页状态不同步

用户在多个标签页打开同一 `ReportDetail`，编辑值不互通（Pinia 是单页 store）。spec 未要求跨标签页同步，可接受。

---

## 5. 下一步行动项

- **M2.12 集成测试**：真实年报端到端验证
  - 引入 Testcontainers + Playwright（M2.12 整合发版前一次性配置）
  - 覆盖关键路径：上传 → 解析 → 抽取 → 写库 → 详情页查看三表 → 编辑 → 切换 Tab
  - 验证 spec §12.1 SLA：PARSE 90s + EXTRACT 60s + CHECK 30s + REPORT 45s < 4min
- **M3 勾稽与异常检测**：消费 `financial_statement` 数据，前端可能扩展"勾稽异常"高亮显示
- **M4 报告生成**：复用 `/statements` 数据生成 NLG 报告
- **可选优化**：
  - 编辑值用 `localStorage` 持久化（key 含 reportId），跨刷新保留
  - 新增 `/reports/{id}/export` 端点导出 Excel 三表
  - 新增"对比模式"展示同期/上期差异
