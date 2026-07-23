# 决策记录：M3.09 前端勾稽页 / 异常页 / 报告页

日期：2026-07-23
主题：M3.09 前端详情页 Tab 扩展与报告产物消费

## 背景

M3.08 完成后，L2 已能把 L3 REPORT 阶段产出的 PDF / Markdown / 3 张图表 PNG 上传到 MinIO，并在 `report_artifact` 表中记录 object key 与状态。M3.09 的目标是在前端详情页展示勾稽结果、异常检测结果以及完整报告产物，并补齐对应的 Controller 层测试。

## 决策列表

1. **M3.09 后端 API 已就绪，本次主要补全前端消费与 Controller 单测**
   - `CheckQueryService` / `AnomalyQueryService` / `ArtifactQueryService` 与 `ReportController` 三个端点已在前续会话实现。
   - 本次新增 `ReportControllerM309Test.java`（7 个单测），覆盖正常返回与 `REPORT_NOT_FOUND` 传播。

2. **详情页 Tab 由 3 个扩展为 6 个**
   - 原有：资产负债表 / 利润表 / 现金流量表
   - 新增：勾稽核对 / 异常检测 / 报告
   - 三表 Tab 继续维护 `statementsStore.activeTab`；新增 Tab 不污染 store 状态。
   - 底部「本地编辑未写回」footer 只在三表 Tab 显示。

3. **报告页用预签名 URL 消费产物**
   - Markdown：通过 MinIO 预签名 URL 拉取原文，经项目自研 `renderMarkdown` 安全渲染。
   - 图表：直接用 `<img :src="downloadUrl">` 展示。
   - PDF：用 `<a download>` 触发浏览器下载。
   - 任一产物 `status=FAILED` 时，对应区域显示「暂不可用」，不阻断其他产物。

4. **Markdown 拉取用裸 fetch 而非 axios**
   - MinIO 预签名 GET URL 的签名基于查询参数；项目 axios 拦截器会注入 `Authorization` / `X-Trace-Id`，破坏签名导致 403。
   - 将 `getMarkdownText(url)` 封装在 `api/artifacts.ts`，内部使用 `fetch`，既遵守「API 调用统一走 `src/api/`」的规范，又避免签名被破坏。

5. **Markdown 渲染走轻量自研渲染器，防止 XSS**
   - 不引入 `marked` 等外部依赖。
   - `renderMarkdown` 在最终输出前对所有文本做 `escapeHtml`。
   - 模板中对 `v-html` 使用 `<!-- eslint-disable-next-line vue/no-v-html -->` 并附注释说明 XSS 已处理。

## 已完成的 Checklist

- [x] `ReportViewer.vue` 实现（Markdown + 图表 + PDF 下载）
- [x] `ReportDetail.vue` 扩展 6 个 Tab
- [x] `api/artifacts.ts` 新增 `getMarkdownText`
- [x] `ReportControllerM309Test.java` 7 个单测
- [x] `ReportControllerStatementsTest.java` 构造器同步修复
- [x] L2 全量回归 328 个测试通过
- [x] 前端 lint / type-check 通过
- [x] `docs/progress/m3.md` 更新

## 发现的风险

- `v-html` 使用需持续保证 `renderMarkdown` 对所有动态内容转义；后续若扩展 Markdown 语法（如链接、代码块），必须同步在 `escapeHtml` 之后输出，禁止输出未转义的行内 HTML。
- MinIO 预签名 URL 默认 1 小时有效期；如果用户长时间停留报告页后点击下载/刷新 Markdown，可能遇到 403。M3.10 端到端测试时验证是否需要缩短刷新周期或点击时重新拉取 artifacts 列表。

## 下一步行动项

- M3.10 端到端验证：真实茅台年报 REPORT 完成后，确认 6 个 Tab 数据完整、PDF 可下载、图表可展示、Markdown 渲染正常。
