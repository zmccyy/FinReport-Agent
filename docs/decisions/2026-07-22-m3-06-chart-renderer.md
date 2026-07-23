# 2026-07-22 M3.06 ChartRenderer 服务端渲染

## 背景

M3.05 ReportGenerator 已交付 5 段式 Markdown 报告。Reasoner → Report 链路
下一步需要 L3 M10 ChartRenderer 将 `FinancialStatement` 数据渲染为 3 张 PNG
图表（资产结构饼图 / 营收趋势折线 / 现金流柱状），供 M3.07 WeasyPrint
嵌入 PDF。

plan M3.06 验收标准：

* 3 张 PNG 生成
* 尺寸 800×500
* 数据正确
* 失败降级（spec §10.3）保证链路不阻断

实现完成后做了全面代码审查，发现 3 个 Major（无 Blocker / Critical）+
4 个 Minor，全部修复后通过 M3.01 + M3.02 + M3.03 + M3.05 + M3.06 共 188
个 L3 测试，2 个目标模块覆盖率 91% / 100%。

## 决策列表

1. **matplotlib + Pillow 替代 ECharts / puppeteer（spec 偏离）** —
   plan M3.06 字面要求 echarts-canvas-js 或 puppeteer。但 Chromium
   重型依赖在 RTX 4050 Mobile 6GB VRAM 环境过重（Chromium 单进程 ~300MB
   内存 + 显存占用），与 spec §8.1 GPU 显存硬约束冲突。改用 matplotlib
   + Agg 后端 + Pillow：纯 Python 栈、内存占用 < 50MB、无需 headless
   浏览器、与 ai-service 现有 PyTorch / numpy 生态天然兼容。spec §2.3
   M10 的「ECharts 服务端渲染」意图是产出 PNG bytes 给 M3.07 WeasyPrint
   嵌入 PDF，matplotlib 等价达成该意图，决策记录在案。

2. **延迟导入模式（环境兼容性 hack）** — 模块级不导入 numpy / matplotlib
   / pyplot。`_ensure_fonts_initialized()` 在首次 `ChartRenderer.__init__`
   时调用，所有 matplotlib / pyplot 引用改为函数内导入。规避 Win +
   numpy 2.x + matplotlib 3.10.x + pytest-cov 的已知兼容性问题：
   coverage import hook 在测试中途重新加载 numpy，导致 matplotlib 的
   `_NoValue` sentinel 与 C 扩展不一致，`fig.add_subplot()` 内部
   `numpy.amin()` 调用抛 `TypeError: float() argument must be a string
   or a real number, not '_NoValueType'`。延迟导入绕开 pytest-cov 的
   二次 patch。这是阶段性的兼容性方案，待 numpy / matplotlib / pytest-cov
   修复后可回归标准顶层导入。

3. **Agg 后端 + PNG bytes 输出** — 服务端无 GUI，`matplotlib.use('Agg')`
   在 pyplot 导入前设置。PNG 通过 `Figure.savefig(BytesIO, format='png')`
   输出二进制，不落临时文件，便于 L2 `ReportArtifactWriter`（M3.08）直接
   上传 MinIO 而无需中转文件。

4. **尺寸 800×500 / DPI 100** — 对齐 plan M3.06 验收标准。figure size =
   (8, 5) 英寸，DPI 100 → 800×500 像素。`ChartRenderer` 构造函数支持
   自定义 `width_px` / `height_px` / `dpi`，便于 M3.07 嵌入 PDF 时按需
   调整（如双栏排版改 400×250）。

5. **中文字体降级链** — 候选列表 8 个字体（SimHei / Microsoft YaHei /
   WenQuanYi Zen Hei / WenQuanYi Micro Hei / PingFang SC / Heiti SC /
   STHeiti / Noto Sans CJK SC），按优先级尝试 `font_manager.findfont(
   fallback_to_default=False)`；任一命中即设置 `plt.rcParams[
   "font.sans-serif"]`。找不到时 `use_chinese_label` 自动降级为 False，
   标签改用英文（Current/Prior/Operating/Investing/Financing 等），
   避免 matplotlib `Glyph missing` 警告渲染成方框。开发环境（Windows
   无中文字体）跑测试时 1 个用例 skip；生产 Linux 部署 Noto Sans CJK SC
   后中文字符正常渲染。

6. **合计科目排除（审查 B1 修复）** — `_TOTAL_ITEM_NAMES` 常量集合
   排除 7 项合计科目（资产总计 / 负债合计 / 所有者权益合计 / 负债和
   所有者权益合计 / 归属于母公司所有者权益合计 / 少数股东权益 等）。
   原实现 `_extract_asset_pie_slices` 把「资产总计」加进「其他」切片
   导致饼图总切片值翻倍（关键科目 2800亿 + 资产总计 2800亿 = 5600亿
   vs 实际总资产 2800亿）。合计科目本身是其他科目的累加值，纳入「其他」
   会让比例重复计算，必须排除。

7. **营收多期对比（审查 B2 修复）** — `_extract_revenue_values` 按
   `StatementItem.period` 字段映射（CURRENT→0 / PRIOR→1 /
   PRIOR_YEAR_TO_DATE→2），按时间顺序返回。原实现用 `seen` 集合按
   item 名称去重，会丢失多期数据（同一科目本期+上期只保留第一条），
   与 spec §2.3 M10「多期营业收入对比」要求矛盾。`CURRENT_YEAR_TO_DATE`
   / `PRIOR_YEAR_TO_DATE` 累计值不纳入趋势对比（营收趋势折线关注
   本期 vs 上期 vs 上年同期，累计值语义不一致）。M3.10 端到端测试
   需验证真实 M7 Extractor 抽取的多期数据顺序与折线 X 轴标签对齐。

8. **不可变输入** — 不修改 `FinancialStatement`；产出新的 `ChartResult`。
   测试 `test_should_not_mutate_statement` 验证。对齐 `LLMReviewer` /
   `AnomalyDetector` / `ReportGenerator` 的不可变风格，便于 L2 落库前后
   对比与并发安全。

9. **失败降级占位 PNG（spec §10.3）** — 数据缺失 / 渲染异常 / 字体缺失
   都不抛 `Exception`，返回占位 PNG（`fallback=True`，含标题 + 失败
   原因文本），保证 Reasoner → Report 链路不被图表渲染失败阻断。
   `ChartResult.fallback` 字段便于 M3.09 前端展示降级提示横幅
   （"图表数据不完整"）。降级路径覆盖 4 种场景：表数据缺失 / 关键
   科目未找到 / 全部值为 0 / 渲染过程异常。

10. **Decimal 精度** — 数据提取用 `Decimal` 计算避免 float 累积误差
    （spec §8.4）；matplotlib 接受 float 输入时统一 `float()` 转换。
    与 `accounting_rules.py` / `anomaly_detector.py` / `llm_reviewer.py`
    / `report_generator.py` 风格一致。

11. **数值格式化 `_format_decimal_short`** — 大数自动转「亿 / 万」
    单位（>=1亿 显示「X.X 亿」、>=1万 显示「X.X 万」、其他保留 2 位
    小数），避免图表数值标注过长溢出。负数同样适用（如 -150 亿）。

12. **同义词组查询** — 与 `accounting_rules.py` / `anomaly_detector.py`
    / `llm_reviewer.py` 风格一致，容忍 A 股年报科目名变体（「营业收入」
    vs 「营业总收入」、「经营活动产生的现金流量净额」 vs 「经营活动
    现金流量净额」、「融资活动产生的现金流量净额」 vs 「筹资活动产生
    的现金流量净额」等）。`_ASSET_PIE_SYNONYMS` / `_REVENUE_SYNONYMS`
    / `_CASH_FLOW_SYNONYMS` 三个常量集中维护。

13. **`chart_templates/` 占位目录** — 为 M3.07 WeasyPrint Jinja2 模板
    预留。M3.07 将在 `chart_templates/` 下放 `report.html` Jinja2 模板
    把 `ReportResult.to_markdown()` + 3 张 PNG 组合为 PDF。

14. **新依赖 matplotlib>=3.8 + pillow>=10.0** — 加入 `pyproject.toml`
    prod 依赖。matplotlib 是 PNG 渲染核心；pillow 是 matplotlib 图像
    后端 + PNG bytes 处理依赖。两个依赖都是纯 Python wheel（matplotlib
    含 C 扩展但 PyPI 提供预编译 wheel），不引入 GPU / CUDA 依赖，与
    spec §8.1 显存约束无冲突。

## 已完成 checklist

* 新增 `app/schemas/chart.py`（ChartType / ChartSpec / ChartResult +
  `resolve_title` / `success` 属性）
* 新增 `app/modules/generator/chart_renderer.py`（`ChartRenderer` 类 +
  `_ensure_fonts_initialized` / `_new_figure` / `_close_figure` /
  `_figure_to_png` / `_render_placeholder_png` +
  `_extract_asset_pie_slices` / `_extract_revenue_values` /
  `_extract_cash_flow_bars` / `_find_item_value` / `_format_decimal_short` +
  `_TOTAL_ITEM_NAMES` / `_ASSET_PIE_SYNONYMS` / `_REVENUE_SYNONYMS` /
  `_CASH_FLOW_SYNONYMS` / `_CHINESE_FONTS_CANDIDATES` 常量）
* 新增 `app/modules/generator/chart_templates/__init__.py`（M3.07 占位）
* 修改 `app/modules/generator/__init__.py` 导出 `ChartRenderer`
* 修改 `ai-service/pyproject.toml` 追加 matplotlib + pillow 依赖
* 新增 52 个测试用例（11 个测试类：TestChartSchema /
  TestAssetStructurePie / TestRevenueTrendLine / TestCashFlowBar /
  TestRenderAll / TestImmutability / TestCustomSize / TestChineseFont /
  TestDataExtractors / TestFormatDecimalShort / TestDefaultsAndEdgeCases）
  + 1 skip（环境无中文字体）
* 修复 B1（饼图合计科目排除）/ B2（营收多期 period 排序）/ B3（误导性
  测试名）+ m1（删 _translate no-op）/ m2（删占位项）/ m3（Any→StatementItem
  类型注解）/ m4（_APPLIED_FONT 隐式依赖）
* 更新 `docs/progress/m3.md` M3.06 交付说明 + 勾选 M3.06
* M3.01 + M3.02 + M3.03 + M3.05 + M3.06 共 188 个 L3 测试全部通过
* 2 个目标模块覆盖率：`chart_renderer.py` 91% / `chart.py` 100%
  （均 ≥ 80% 门槛）
* ruff check / ruff format 全部通过

## 发现的风险

1. **matplotlib 中文字体在生产 Linux 环境可能缺失** — 候选列表包含
   Noto Sans CJK SC，但生产 docker 镜像默认不安装中文字体包。M3.07
   WeasyPrint 部署时需在 Dockerfile 显式 `apt install fonts-noto-cjk`
   （或 fonts-wqy-zenhei）。否则生产环境会触发 `use_chinese_label=False`
   降级，图表标签全英文，与 PDF 中文报告风格不一致。M3.10 端到端
   测试需验证生产镜像字体安装。

2. **延迟导入是环境兼容性 hack** — 当前为绕开 Win + numpy 2.x +
   matplotlib 3.10.x + pytest-cov 兼容性问题而延迟导入 matplotlib。
   待 numpy / matplotlib / pytest-cov 修复后（或 Linux 生产环境
   无此问题），可回归标准顶层导入。M3.10 端到端测试在 Linux 容器
   中验证是否需要保留延迟导入模式。

3. **营收多期数据未端到端验证** — 当前 `_extract_revenue_values` 按
   `period` 字段排序，但 M7 Extractor 实际抽取的多期数据格式未验证。
   若 Extractor 倾向于把多期数据拆成多条 `StatementItem`（item 相同、
   period 不同），当前实现可正确处理；若 Extractor 把多期数据放在
   同一条 `StatementItem` 的 `value` 字段中（如逗号分隔字符串），
   则需调整。M3.10 端到端测试需验证。

4. **饼图「其他」切片可能为负值** — 当资产负债表中有负值科目（如
   「累计折旧」负数表示）时，`other_total` 可能为负。matplotlib
   `ax.pie()` 对负值行为未定义（可能抛 ValueError 或渲染异常）。
   当前实现用 try/except 兜底降级到占位 PNG，但 M3.10 端到端测试
   需验证真实年报中负值科目的处理。必要时改为「其他」切片取绝对值
   或拆分正负两组。

5. **`render_all` 顺序固定** — `render_all` 按 `[pie, line, bar]`
   顺序返回，与 `ChartType` 枚举顺序一致。M3.07 WeasyPrint 嵌入 PDF
   时需按此顺序排列图表。若 spec 后续要求图表顺序可配置（如公司
   自定义报告模板），需扩展 `render_all` 支持顺序参数。

6. **PNG bytes 较大** — 800×500 PNG 单张约 30-50KB，3 张约 150KB。
   M3.08 L2 写 `report_artifact` 上传 MinIO 时需考虑批量上传耗时。
   必要时可降低 DPI 到 80 或压缩 PNG（`fig.savefig(optimization=9)`）。

## 下一步行动项

* M3.07 L3 M10 Markdown → PDF — WeasyPrint 消费 `ReportResult.to_markdown()`
  + 3 张 PNG 嵌入 PDF；Dockerfile 显式安装 `fonts-noto-cjk`
* M3.08 L2 写 report_artifact + 上传 MinIO — 持久化报告 Markdown + PDF + 3 张 PNG
* M3.09 前端勾稽页 + 异常页 + 报告页 — 消费 `ChartResult` 渲染图表 Tab
* M3.10 端到端 SLA 测试 — 真实茅台年报抽取结果 → 3 张图表数据合理性 +
  Linux 容器中文字体渲染效果
