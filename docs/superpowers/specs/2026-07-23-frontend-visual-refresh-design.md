# 前端界面美化设计文档：现代 SaaS 浅色风

日期：2026-07-23
主题：FinReport Agent L1 前端全面美化（M3.09 后续优化）

## 1. 设计方向

采用 **现代 SaaS 浅色风**，参考 Linear、Notion、Raycast、Clerk 等 2024–2026 年主流 B2B SaaS 产品。

核心特征：

- **背景**：极浅的暖灰白（`#fafafa` / `#f7f7f8`），替代纯白，降低刺眼感
- **卡片**：白色卡片 + 极淡阴影 + 1px 极浅边框，悬浮时轻微上浮
- **圆角**：大圆角（12–16px 卡片，8–10px 按钮/输入框），柔和现代
- **字体**：系统无衬线 + `Inter` / `PingFang SC` / `Microsoft YaHei`，数字等宽
- **强调色**：保留当前品牌蓝，但降低饱和度、增加清透感；用青绿表示成功、琥珀表示警告、珊瑚红表示危险
- **数据密度**：中等偏高，关键数字放大、标签缩小，形成清晰层级
- **微交互**：Tab 切换有下划线滑动、卡片 hover 上浮、按钮 press 下沉、骨架屏 shimmer

## 2. 设计系统（Design Tokens）

```css
:root {
  /* 背景层级 */
  --fin-bg: #f7f7f8;
  --fin-surface: #ffffff;
  --fin-surface-elevated: #ffffff;

  /* 文字层级 */
  --fin-text-primary: #111113;
  --fin-text-regular: #2c2c2e;
  --fin-text-secondary: #6e6e78;
  --fin-text-tertiary: #a0a0aa;

  /* 边框与分隔 */
  --fin-border: #ededf0;
  --fin-border-strong: #e4e4e9;
  --fin-divider: #f0f0f3;

  /* 品牌色（清透蓝） */
  --fin-primary: #2563eb;
  --fin-primary-light: #3b82f6;
  --fin-primary-bg: #eff6ff;
  --fin-primary-subtle: rgba(37, 99, 235, 0.08);

  /* 状态色 */
  --fin-success: #10b981;
  --fin-success-bg: #ecfdf5;
  --fin-warning: #f59e0b;
  --fin-warning-bg: #fffbeb;
  --fin-danger: #ef4444;
  --fin-danger-bg: #fef2f2;
  --fin-info: #6366f1;

  /* 圆角 */
  --fin-radius-xs: 6px;
  --fin-radius-sm: 10px;
  --fin-radius-md: 14px;
  --fin-radius-lg: 18px;

  /* 阴影 */
  --fin-shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04);
  --fin-shadow-md: 0 4px 12px rgba(0, 0, 0, 0.05);
  --fin-shadow-lg: 0 12px 32px rgba(0, 0, 0, 0.08);
  --fin-shadow-card-hover: 0 8px 24px rgba(0, 0, 0, 0.07);
}
```

## 3. 关键界面改造点

### 3.1 全局布局

- `AppHeader`：改为半透明毛玻璃效果（`backdrop-blur`），底部 1px 细线，左侧 Logo + 产品名，右侧用户头像
- 页面容器：`max-width: 1280px`，居中带 24px 侧边距
- 所有卡片统一 `.fin-card`：white bg + 14px radius + 1px border + `shadow-sm`

### 3.2 ReportDetail 头部

- 公司名：28px 加粗，黑色
- 元数据行：灰色小字，标签胶囊化（年报 / 2024-12-31 / 156 页）
- 状态标签：更小的 pill 形状，颜色按状态
- 操作按钮：primary 按钮用圆角实色，secondary 用 ghost 样式
- 新增「快捷指标卡」横向排列：总资产、净利润、经营现金流（占位，未来接 L3 摘要接口）

### 3.3 Tab 导航

- 移除 `type="border-card"`，改用底部下划线样式
- 激活态：品牌蓝 + 2px 下划线
- 悬浮态：背景色轻微变化
- Tab 内容切换加 `fade-slide` 过渡动画

### 3.4 三表（StatementTable）

- 表头：浅灰背景 + 小字大写标签
- 行 hover：浅蓝背景高亮
- 数字右对齐、等宽字体
- 可编辑单元格：聚焦时蓝色边框 + 柔和 glow
- 空状态：插画 + 提示文案

### 3.5 勾稽核对（CheckList）

- 卡片左侧用彩色竖条区分状态：通过=绿、失败=红、未知=灰
- 头部图标改为圆角图标容器（icon inside rounded square）
- 指标三列：预期/实际/差额，差额非 0 时标红/黄
- 整体布局从垂直堆叠改为响应式 grid（大屏 2 列）

### 3.6 异常检测（AnomalyList）

- 与 CheckList 保持卡片设计语言一致
- 严重级别用左侧竖条 + 右上角小标签双重表达
- 描述文字增加可读性，限制最大宽度

### 3.7 报告页（ReportViewer）

- Markdown 渲染区：更优雅的排版（标题层级、引用块、代码块）
- 图表区：卡片带 hover 放大效果，图表标题 + 类型标签更精致
- PDF 下载按钮：突出的 primary CTA
- 整体分栏：左侧 Markdown，右侧图表（大屏）

## 4. 图像生成提示词

用于 Midjourney / Stable Diffusion / Flux 等工具生成 UI 概念图、风格板、配色参考。

### 4.1 整体仪表盘概念图

```text
A modern SaaS financial report dashboard UI, light theme, clean minimal design,
pale warm gray background #f7f7f8, white rounded cards with subtle shadow and 1px light border,
Chinese financial data interface, top navigation bar with frosted glass blur effect,
company header card showing "Kweichow Moutai Co., Ltd." with metadata pills,
six-tab navigation with underline active state,
data tables for balance sheet, income statement, cash flow,
check result cards and anomaly alert cards in grid layout,
soft blue accent color #2563eb, green success, amber warning, coral danger,
Inter font, generous whitespace, high readability,
web design, UI/UX, Figma mockup, 4K, professional
--ar 16:9 --style raw --v 6
```

### 4.2 报告详情页局部

```text
Close-up of a financial report detail page UI, light modern SaaS style,
white card with 14px rounded corners, soft shadow,
company name "贵州茅台" in bold black Chinese typography,
small gray metadata pills: 年报 · 2024-12-31 · 156页,
status pill tag "已完成" in green,
segmented tab bar with active blue underline,
Below: a data table with headers 科目 / 金额 / 币种 / 置信度,
row hover light blue highlight, numbers in monospace font right aligned,
clean grid, professional fintech aesthetic, Figma, dribbble, 4K
--ar 16:9 --style raw --v 6
```

### 4.3 勾稽核对卡片

```text
UI card component for accounting validation results, light SaaS theme,
white rounded card, subtle top colored border stripe,
left side: rounded square icon container green checkmark,
title "资产负债表恒等式", severity tag "错误" in coral,
three metrics in a row: 预期值 / 实际值 / 差额,
failed metric highlighted in red,
description text below, clean typography,
Figma component, isolated on light gray background, 4K
--ar 4:3 --style raw --v 6
```

### 4.4 报告产物页（含图表）

```text
Financial report output page UI, light modern SaaS design,
left side: rendered Markdown report with Chinese text, elegant heading hierarchy,
right side: chart grid showing pie chart, line chart, bar chart inside white cards,
each chart card has rounded corners, subtle shadow, chart type tag "饼图",
blue download PDF button at top right,
pale gray background, high contrast, professional, Figma mockup, 4K
--ar 16:9 --style raw --v 6
```

## 5. 代码生成提示词（给 Claude / v0 / Cursor）

### 5.1 整体改造提示词

```text
你是一名资深 Vue3 + TypeScript + Element Plus 前端工程师。请基于以下要求，
对项目 frontend/src 下的 FinReport Agent 前端进行全面视觉升级，采用现代 SaaS 浅色风。

设计系统：
- 背景：--fin-bg: #f7f7f8
- 卡片：--fin-surface: #ffffff，14px 圆角，1px #ededf0 边框，0 1px 2px rgba(0,0,0,0.04) 阴影
- 文字主色：--fin-text-primary: #111113；次色：--fin-text-secondary: #6e6e78
- 强调色：--fin-primary: #2563eb
- 状态色：success=#10b981, warning=#f59e0b, danger=#ef4444
- 圆角：按钮 8px，输入框 8px，卡片 14px，大卡片 18px
- 字体：系统字体 + Inter fallback，数字用 SFMono / Consolas

必须修改的文件和具体改动：
1. frontend/src/styles/variables.css：替换/新增上述 CSS 变量
2. frontend/src/App.vue / AppHeader.vue：header 改为半透明毛玻璃（backdrop-blur-md），底部细线，左侧 Logo+产品名，右侧用户头像
3. frontend/src/views/ReportDetail.vue：
   - 头部公司名 28px bold，元数据改为胶囊 pill
   - 状态标签使用小型 pill
   - 新增三个「关键指标卡」横向排列（总资产/净利润/经营现金流），先 mock 数据占位
   - Tab 改用自定义下划线样式，移除 border-card
   - Tab 内容切换加 fade-slide 动画
   - 所有卡片统一 fin-card
4. frontend/src/components/StatementTable.vue：
   - 表头浅灰背景、小字大写标签
   - 行 hover 浅蓝背景
   - 数字右对齐等宽
   - 可编辑单元格聚焦时蓝色边框 glow
5. frontend/src/components/CheckList.vue：
   - 卡片左侧用彩色竖条（通过=绿，失败=红）
   - 图标改为圆角方形图标容器
   - 大屏下改为 2 列 grid 布局
6. frontend/src/components/AnomalyList.vue：
   - 与 CheckList 统一卡片语言
   - 严重级别左侧竖条 + 右上角小标签
7. frontend/src/components/ReportViewer.vue：
   - Markdown 区排版优化（标题、列表、表格、引用块）
   - 图表区 hover 轻微放大
   - PDF 下载按钮改为突出 CTA
   - 大屏下 Markdown 左、图表右分栏
8. 全局：按钮、输入框、标签、消息提示统一圆角和颜色

约束：
- 继续使用 Element Plus，不要引入新 UI 库
- 不要修改业务逻辑、API 调用、类型定义
- scoped CSS，使用 CSS 变量
- 保持响应式，支持 1280px+ 大屏和 768px 以下平板
- 所有代码带中文注释说明改动原因
- 改完后运行 npm run lint 和 npm run type-check 必须无错

请按文件逐个输出完整可运行代码。
```

### 5.2 单页面改造提示词（ReportDetail）

```text
请帮我重构 frontend/src/views/ReportDetail.vue 的视觉样式，保持现有业务逻辑不变。

目标风格：现代 SaaS 浅色风，参考 Linear / Notion。

具体改动清单：
1. 头部 `.page__head`：改为白色圆角卡片，内部 flex 布局，公司名加大到 28px 加粗黑色。
2. 元数据 `.page__sub`：把 reportType / reportPeriod / pageCount 改为独立小胶囊 pill，
   背景 --fin-primary-subtle，文字 --fin-primary，8px 圆角。
3. 操作区：「查看进度」改为 ghost 按钮，「返回列表」改为次要按钮；状态标签改为小 pill。
4. 在 `.page__meta` 上方新增一行 `.kpi-bar`，横向排列 3 个 KPI 卡片：
   - 总资产 ¥1,234,567.89
   - 净利润 ¥234,567.89
   - 经营现金流 ¥345,678.90
   每个 KPI 卡片：白色背景、14px 圆角、小字标签 + 大字数值。
5. `.page__meta`：改为更紧凑的横向卡片，标签用 11px 大写灰色。
6. `el-tabs`：移除 `type="border-card"`，改用自定义 class `.fin-tabs-underline`：
   - Tab 项横向排列，间距 24px
   - 激活态：文字 --fin-primary，底部 2px 蓝色下划线
   - 悬浮态：背景轻微变色
7. Tab 内容区加 `.tab-content` 包裹，切换时 200ms fade-slide 动画。
8. `.statements-card` 内部 padding 改为 20px，卡片本身用 fin-card。
9. 底部 `.statements-card__foot`：改为右对齐紧凑条带，编辑提示用橙色 subtle 背景 pill。

颜色变量假设已在 variables.css 中定义，可直接使用 var(--fin-*) 变量。
请输出完整 `<template>` 和 `<style scoped>` 代码，不要改动 `<script setup>` 逻辑。
```

### 5.3 组件改造提示词（CheckList）

```text
请重构 frontend/src/components/CheckList.vue 的视觉样式，保持 script 逻辑和 API 调用不变。

目标：把当前垂直堆叠的卡片改为现代 SaaS 风格的验证结果卡片，大屏时 2 列网格。

改动：
1. `.check-list__cards`：大屏 `grid-template-columns: repeat(2, 1fr)`，gap 16px；移动端单列。
2. `.check-card`：
   - 白色背景，14px 圆角，1px #ededf0 边框，阴影 shadow-sm
   - 左侧 4px 彩色竖条：通过=绿色，失败=红色，未知=灰色
   - hover 时轻微上浮 shadow-md
3. `.check-card__head`：左侧图标改为 32px 圆角方形容器，内部图标 18px；标题 15px semibold。
4. `.check-card__metrics`：改为 3 等分 flex，无边框，指标标签 11px 灰色大写，数值 14px 等宽。
5. 差额非 0 时数值标红/黄，并加粗。
6. `.check-card__note`：改为浅灰背景 subtle 提示块，12px 圆角。
7. 空状态、错误状态、加载状态保持现有结构，但图标和文字居中，加载图标用品牌蓝。

请输出完整 `<template>` 和 `<style scoped>`。
```

### 5.4 图表/Markdown 改造提示词（ReportViewer）

```text
请重构 frontend/src/components/ReportViewer.vue，保持现有数据加载逻辑不变。

目标：让报告产物页更像现代 SaaS 文档阅读器。

改动：
1. 顶部 actions：「下载 PDF」按钮改为突出的 primary CTA 按钮（圆角 8px，蓝色实色，带图标）。
2. 整体布局：
   - 大屏（>=1280px）：左侧 Markdown 占 7 列，右侧图表占 5 列
   - 中屏（>=768px）：单列，Markdown 在上，图表在下
3. Markdown 渲染区 `.report-viewer__markdown`：
   - 白色卡片，18px 圆角，28px padding
   - 标题层级清晰：H2 18px bold 带底部 border，H3 15px semibold
   - 段落行高 1.75，字体 14px
   - 表格：表头浅蓝背景，斑马纹，12px 圆角 overflow hidden
   - 列表：自定义蓝色 bullet
4. 图表区 `.charts__grid`：
   - 改为 1 列（大屏右侧单列展示），每个 chart-card 16px 圆角
   - hover 时 `transform: translateY(-2px)` + shadow-md
   - 图表图片带圆角下方
   - 头部图标容器 28px 圆角方形，标题 14px semibold
5. 空状态、错误状态、加载状态使用统一插画风格占位（可用 emoji 或 Element Plus 图标）。

输出完整 `<template>` 和 `<style scoped>`，script 部分不需要改动。
```

## 6. 实施建议

1. **先跑图像提示词**：用 Midjourney / Flux 生成 2–3 张概念图，确定视觉方向。
2. **再跑代码提示词**：把本设计文档 + 当前源码一起喂给 Claude / v0 / Cursor，让它按文件输出。
3. **验收标准**：
   - `npm run lint` 无错
   - `npm run type-check` 通过
   - 浏览器中 ReportDetail / CheckList / AnomalyList / ReportViewer 四个界面视觉一致
   - 深色模式不是本次范围，但 CSS 变量结构应预留扩展

## 7. 来源参考

- 搜索过程中参考了 2026 年数据可视化、金融大屏、SaaS Dashboard 相关中文与英文资料
- 风格灵感来自 Linear、Notion、Raycast、Clerk 等主流 B2B SaaS 产品
- Element Plus 主题定制参考官方文档
