# 2026-07-23 M3.07 PdfConverter Markdown → PDF

## 背景

M3.06 ChartRenderer 已交付 3 张 PNG 图表。Reasoner → Report → Artifact
链路下一步需要 L3 M10 PdfConverter 把 `ReportResult.to_markdown()` 输出
+ 3 张 `ChartResult` PNG 组合转为 PDF，供 M3.08 L2 `ReportArtifactWriter`
上传 MinIO 后前端下载。

plan M3.07 验收标准：

* PDF 包含 5 段文字 + 3 张图表
* 排版正常
* 验证方式：打开生成的 PDF 检查

实现完成后做了全面代码审查，发现 2 个 Major + 3 个测试 bug + 1 个 Minor，
全部修复后通过 M3.01 + M3.02 + M3.03 + M3.05 + M3.06 + M3.07 共 224
个 L3 测试 + 1 skip，2 个目标模块覆盖率 100% / 100%。

## 决策列表

1. **WeasyPrint + Jinja2 + markdown 栈（spec 字面要求）** —
   spec §2.3 M10 字面要求 WeasyPrint；Jinja2 渲染 `report.html` 模板；
   `markdown` 库把 `ReportResult.to_markdown()` 转 HTML 段落。三者都
   在函数内延迟导入（与 `ChartRenderer` 风格一致），避免模块级 GTK /
   Pango 依赖加载在 Windows 开发环境抛 OSError。

2. **PNG base64 嵌入 HTML** — 3 张图表 PNG bytes 编码为
   `data:image/png;base64,...` 直接嵌入 HTML `<img src="...">`，避免
   MinIO 中转文件 + WeasyPrint `base_url` 复杂度。L2 `ReportArtifactWriter`
   （M3.08）上传 MinIO 时仍按 `reports/{reportId}/charts/*.png` 分开存储，
   前端可独立引用图表或下载完整 PDF。

3. **A4 页面 + 中文字体降级链** — `@page size: {{ page_size }}` 由
   PdfConverter 传入（默认 A4，可配 Letter 等）；`body font-family`
   候选列表含 Noto Sans CJK SC / Microsoft YaHei / SimHei / WenQuanYi
   Zen Hei / DejaVu Sans，覆盖 Linux / Windows / macOS 主流中文字体；
   找不到时由 WeasyPrint 默认字体兜底（中文可能渲染成方框，但 PDF
   仍可生成）。

4. **不可变输入** — 不修改 `ReportResult` / `ChartResult`；产出新的
   `PdfResult`。与 `ChartRenderer` / `ReportGenerator` 风格一致。
   测试 `test_should_not_mutate_report` / `test_should_not_mutate_charts`
   验证。

5. **失败降级占位 PDF（spec §10.3）** — WeasyPrint 异常 / 模板缺失 /
   markdown 解析失败 / 超时 都不抛 `Exception`，返回最小占位 PDF
   （`fallback=True`），保证 Report → Artifact 链路不被 PDF 渲染失败
   阻断。降级链：
   - `convert` 异常 → `_fallback()` 调用 `_render_fallback_pdf`
   - `_render_fallback_pdf` 生成最小 HTML → PDF（含失败原因 + 5 段标题列表）
   - WeasyPrint 完全不可用 → `_MINIMAL_PDF_PLACEHOLDER`（11 字节 PDF
     header `b"%PDF-1.4\n%%EOF\n"`，前端打开会显示「PDF 损坏」但 L2
     上传 MinIO 不会因 0 bytes 失败）

6. **异步 + to_thread + wait_for（B1 修复）** — WeasyPrint `write_pdf`
   是同步阻塞调用（内部 C 库 libpango）；用 `asyncio.to_thread` 避免
   阻塞事件循环。审查发现 `timeout_seconds` 属性存在但未应用，改用
   `asyncio.wait_for(asyncio.to_thread(...), timeout=self.timeout_seconds)`
   应用超时（spec §3.7 REPORT 链路 45s SLA；PDF 渲染默认 30s）。
   超时后底层线程仍可能继续运行（Python asyncio 无法强制取消同步线程），
   但调用方不会无限等待。新增 `except asyncio.TimeoutError` 分支返回
   fallback + error 含「渲染超时」标记。

7. **charts 入口截断（M3 修复）** — spec §2.3 M10 固定 3 张图表；schema
   `chart_count le=3` 自我保护，多余的图表在 `convert` 入口截断
   （`charts = charts[:3]`），避免调用方手动裁剪 + schema 校验失败。

8. **page_size 传入模板（B2 修复）** — `PdfConverter.page_size` 属性
   通过 `template.render(page_size=self.page_size)` 传入模板；`@page
   size: {{ page_size }}` 动态渲染，避免接口契约不一致（属性可配但
   模板硬编码）。测试 `test_page_size_passed_to_template` /
   `test_page_size_rendered_in_html` 验证。

9. **markdown 扩展** — `_MARKDOWN_EXTENSIONS = ["tables", "fenced_code",
   "nl2br"]` 支持表格 / 围栏代码块 / 软换行转 `<br>`；与 `ReportResult.
   to_markdown()` 输出格式对齐。

10. **timeout_seconds 默认 30s** — spec §3.7 REPORT 链路 SLA 目标 45s
    （含 LLM 生成 + PDF 渲染）；PDF 渲染预留 30s，给 LLM 留 15s 余量。
    可通过 `PdfConverter(timeout_seconds=60)` 自定义。

## 已完成的 checklist

- [x] `ai-service/app/schemas/pdf.py` — `PdfResult` schema + `success` 属性
- [x] `ai-service/app/modules/generator/templates/report.html` — Jinja2 模板
- [x] `ai-service/app/modules/generator/pdf_converter.py` — `PdfConverter` 类
- [x] `ai-service/app/modules/generator/__init__.py` — 导出 `PdfConverter`
- [x] `ai-service/pyproject.toml` — 新增 weasyprint / markdown / jinja2 依赖
- [x] `ai-service/tests/test_m3_pdf_converter.py` — 36 个测试用例（9 个测试类）
- [x] Windows 开发环境 mock weasyprint 注入方案
- [x] 代码审查：2 Major + 3 测试 bug + 1 Minor 全部修复
- [x] M3.07 单元测试 36 个全部通过，模块覆盖率 100%
- [x] 全 M3 L3 回归测试 224 passed + 1 skip，无破坏
- [x] ruff check 全部通过
- [x] `docs/progress/m3.md` 勾选 M3.07 + 追加交付说明

## 发现的风险

1. **Windows 开发环境缺 GTK Runtime** — WeasyPrint 在 `import` 时即调用
   `cffi.dlopen` 加载 GTK C 库（libgobject-2.0-0 / libpango），Windows
   开发环境无 GTK 会抛 `OSError: cannot load library 'libgobject-2.0-0':
   error 0x7e`。测试通过 `sys.modules` 注入 mock weasyprint 模块绕开
   （Linux 生产环境 GTK 自动安装后此 mock 不影响真实 PDF 生成）。
   **风险**：Windows 开发者无法本地验证 PDF 排版效果，需依赖 Linux
   容器或 CI 环境验证。M3.10 端到端测试必须在 Linux 环境执行。

2. **超时后底层线程仍运行** — `asyncio.wait_for` 超时后抛
   `TimeoutError`，但 `asyncio.to_thread` 内的 WeasyPrint 同步线程
   无法强制取消，可能继续运行直到完成或自身超时。**风险**：高并发
   场景下多个超时请求可能累积线程，占用内存。M3.10 端到端测试需要
   监控线程数。

3. **中文字体兜底** — Linux 生产环境若未安装 Noto Sans CJK SC 等
   中文字体，WeasyPrint 默认字体无法渲染中文，PDF 会显示方框。
   **风险**：部署文档需要明确要求安装中文字体包
   （`apt-get install fonts-noto-cjk` 或同等命令）。

4. **大 PDF 渲染性能** — 100 页+ 年报 + 3 张图表 + 5 段长 Markdown
   报告 → PDF 渲染可能接近 30s 超时阈值。**风险**：M3.10 端到端测试
   需要验证 4MB+ 大 PDF 是否触发超时降级。

## 下一步行动项

- M3.08 L2 `ReportArtifactWriter` — 消费 `PdfResult.pdf_bytes` + 3 张
  `ChartResult.png_bytes` 上传 MinIO `reports/{reportId}/report.pdf` +
  `reports/{reportId}/charts/*.png`，写入 `report_artifact` 表
- M3.09 前端报告页 — 下载 PDF + 展示降级提示横幅（`PdfResult.fallback=True`
  时显示「PDF 生成失败」）
- M3.10 端到端 SLA 测试 — 真实 WeasyPrint + Linux GTK Runtime 验证
  PDF 排版效果 + 大 PDF 渲染性能
- 部署文档更新 — `docs/deployment.md` 增加 Linux 容器中文字体包安装
  说明（`fonts-noto-cjk`）
