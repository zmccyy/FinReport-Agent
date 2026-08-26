# M6.01 / M6.02 前端打磨与性能优化决策记录

日期：2026-08-26
任务：M6.01 前端 UI 打磨、M6.02 前端性能优化

## 背景

M6.01 计划要求"响应式布局、加载态/空态/错误态、暗黑模式"（验收：3 种屏幕尺寸
布局正常、所有异步操作有加载态）；M6.02 计划要求"路由懒加载、组件缓存、
SSE 背压处理"（验收：首屏 < 2s，Lighthouse ≥ 90）。
工作区已有部分 WIP（useTheme、三态组件雏形、AppHeader 响应式），
本对话审查后补齐、修复并对暗黑模式逐处适配。

## 决策列表

1. **主题机制：html.dark class + --fin-* CSS 变量全覆盖**
   - 浅/暗各一套完整 token（含此前遗漏的 `--fin-*-subtle`、语义色提亮、
     `--fin-fill-muted`、`color-scheme` 原生控件配色）。
   - Element Plus dark css-vars 内的 `--el-color-primary` 是 EP 默认蓝，
     需在 `html.dark` 后声明覆盖回品牌蓝（与 `:root` 层对齐）。
   - 防 FOUC：index.html 内联脚本在 CSS/JS 就绪前预置 html.dark，
     逻辑与 useTheme.ts 保持单一事实来源一致。
   - 首启跟随系统偏好（prefers-color-scheme），手工切换写入
     localStorage（`finreport-theme`）。

2. **登录页（公开态）也提供主题切换**
   - 计划仅提及页面打磨，但 AppHeader 只在登录后出现：暗色用户登入前
     无法切换主题。新增通用 `ThemeToggle.vue`，AppHeader 与 Login 共用。

3. **三态收敛为通用组件**
   - `EmptyState` / `ErrorState` / `PageLoading`（components/common/）；
     Reports 空态、ReportDetail 加载/错误态接入，删除自绘重复样式。
     勾稽/异常/报告 Tab 因已有各自 loading/error/empty 且带业务提示，
     保留现状不动（避免回归）。

4. **SSE 背压语义：buffer 64 达到即阻塞**
   - push 后缓冲 ≥ 64 时返回 Promise 阻塞调用方（reader.read 暂停，
     网络层背压），25ms 定时器成批 flush（Vue 状态更新合并）。
   - 终态事件也走队列（≤25ms 延迟），flush 先 dispatch 后放行等待者，
     保证调用方恢复时已见 terminal 标志。

5. **SseParser 拆分为零依赖模块并修复解析 bug**
   - 原 sse.ts 顶层读取 `import.meta.env`，Node 下无法直载，解析器无法单测；
     拆出 `sse-parser.ts`（sse.ts re-export 保持兼容）。
   - 修复：原实现"优先按 \n 切行"，孤 \r 行尾与后续 \n 混合时行被粘合；
     改为从左到右取最小行尾分隔符，保留跨 chunk CRLF 延后语义。

6. **Element Plus 全量引入保持（不按需化）**
   - 全量注册图标 + 全量 CSS 自 M1 沿袭，按需化需改造图标用法与新增
     unplugin 依赖（计划外），回归风险大于首屏收益（本地/内网部署）。
   - 折中：manualChunks 拆 vendor（element-plus / vue 系 / 应用互为长缓存），
     vendor-element ≈ 1.07MB（gzip 339KB）属预期，调高 chunkSizeWarningLimit。
   - Lighthouse ≥ 90 与首屏 < 2s 的复验放在 M6.11 干净环境一键启动时。

7. **路由懒加载**：本任务巡检确认 M1 起全路由已 `() => import()`，无需改动。

## 已完成清单

- [x] lint / type-check / build 全绿
- [x] node --test 单测 14 例（背压队列 5 + SseParser 9）全绿
- [x] Playwright E2E 9 例全绿（无后端可跑：路由守卫/登录表单/主题切换与
      持久化/空态/375px 响应式/SSE mock 流终态）
- [x] 375 / 1280（明暗两套）截图人工复核无浅色残留
- [x] M6.01、M6.02 各自单提交（dc7712c / a511aec，Conventional Commits）

## 风险与下一步

- Chat 流 token 高频时背压下限 25ms 批次，视觉上 token 到达略成批（无感）；
  若实测体感不佳可调 intervalMs。
- Playwright 浏览器与 devDependency 属新增（AGENTS §6.2 已预设 E2E 角色）。
- M6.11 干净环境启动时复验首屏时间与 Lighthouse。
