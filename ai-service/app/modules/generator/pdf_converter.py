"""M10 Markdown → PDF 转换器（spec §2.3 M10 + plan M3.07）。

职责：

* 消费 ``ReportResult.to_markdown()`` 输出 + ``ChartResult`` 列表，
  通过 Jinja2 渲染 HTML 模板 → WeasyPrint 转 PDF。
* 5 段报告正文：Markdown → HTML（``markdown`` 库，支持表格 / 列表 / 加粗
  等基础语法）。
* 3 张图表：PNG bytes → base64 data URL 嵌入 HTML，避免临时文件。
* 失败降级（spec §10.3）：WeasyPrint 异常 / 字体缺失 / 渲染失败 都不抛，
  返回最小占位 PDF（``fallback=True``），保证 Report → Artifact 链路
  不被 PDF 渲染失败阻断。

设计要点：

1. **WeasyPrint + Jinja2 + markdown** — spec 字面要求（spec §2.3 M10）：
   WeasyPrint 把 HTML/CSS 转 PDF；Jinja2 渲染 ``report.html`` 模板；
   ``markdown`` 库把 ``ReportResult.to_markdown()`` 转 HTML 段落。
2. **PNG base64 嵌入** — 3 张图表 PNG bytes 编码为
   ``data:image/png;base64,...`` 直接嵌入 HTML ``<img src="...">``，
   避免 MinIO 中转文件 + WeasyPrint base_url 复杂度；L2 ``ReportArtifactWriter``
   （M3.08）上传 MinIO 时仍按 ``reports/{reportId}/charts/*.png`` 分开存储。
3. **A4 页面 + 中文字体** — ``@page size: A4`` + ``body font-family``
   候选列表含 Noto Sans CJK SC / Microsoft YaHei / SimHei，覆盖
   Linux / Windows / macOS 主流中文字体；找不到时由 WeasyPrint 默认
   字体兜底（中文可能渲染成方框，但 PDF 仍可生成）。
4. **不可变输入** — 不修改 ``ReportResult`` / ``ChartResult``；产出
   新的 ``PdfResult``。与 ``ChartRenderer`` / ``ReportGenerator`` 风格一致。
5. **失败降级占位 PDF** — WeasyPrint 失败时由 ``_render_fallback_pdf``
   生成最小 PDF（仅含标题 + 失败原因文本），便于 M3.08 L2 上传 MinIO
   后前端展示「PDF 生成失败」而非空白。
6. **段落数据截断** — Markdown 转 HTML 时若段内容为空，渲染为空
   ``<p></p>`` 占位避免 Jinja2 模板渲染异常。
7. **异步 + to_thread** — WeasyPrint ``write_pdf`` 是同步阻塞调用
   （内部 C 库 libpango）；用 ``asyncio.to_thread`` 避免阻塞事件循环，
   不引入并发（与 ``ReportGenerator`` / ``LLMReviewer`` 风格一致）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from pathlib import Path
from typing import Any

from app.schemas.chart import ChartResult
from app.schemas.pdf import PdfResult
from app.schemas.report import ReportResult
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# 模板目录（相对于本文件）。
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TEMPLATE_NAME = "report.html"

# WeasyPrint 渲染超时（spec §3.7 REPORT 链路 45s SLA；PDF 渲染预留 30s）。
_DEFAULT_TIMEOUT_SECONDS = 30.0

# 默认页面规格。
_DEFAULT_PAGE_SIZE = "A4"

# markdown 扩展：支持表格 + 列表 + 加粗等基础语法。
_MARKDOWN_EXTENSIONS = ["tables", "fenced_code", "nl2br"]


class PdfConverter:
    """Markdown → PDF 转换器（spec §2.3 M10 + plan M3.07）。

    Attributes:
        template_dir: Jinja2 模板目录。
        page_size: 页面规格（默认 A4）。
        timeout_seconds: WeasyPrint 渲染超时。
    """

    def __init__(
        self,
        *,
        template_dir: Path | None = None,
        page_size: str = _DEFAULT_PAGE_SIZE,
        timeout_seconds: float | None = None,
    ) -> None:
        """初始化 PDF 转换器。

        Args:
            template_dir: Jinja2 模板目录；默认使用 ``templates/`` 子目录。
            page_size: 页面规格（A4 / Letter 等）；默认 A4。
            timeout_seconds: WeasyPrint 渲染超时；默认 30s。
        """
        self.template_dir = template_dir if template_dir is not None else _TEMPLATE_DIR
        self.page_size = page_size
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else _DEFAULT_TIMEOUT_SECONDS
        )

    async def convert(
        self,
        report: ReportResult,
        charts: list[ChartResult] | None = None,
        *,
        company_name: str = "",
        company_code: str = "",
    ) -> PdfResult:
        """把 ReportResult + 图表转为 PDF。

        Args:
            report: 5 段式报告结果。
            charts: 图表列表（最多 3 张）；None 视为无图表。
            company_name: 公司名称（用于报告标题）。
            company_code: 公司股票代码。

        Returns:
            ``PdfResult``；WeasyPrint 成功时 ``fallback=False`` + ``pdf_bytes``
            为 PDF 二进制；失败时 ``fallback=True`` + 占位 PDF。
        """
        start = time.monotonic()
        charts = charts or []
        # spec §2.3 M10 固定 3 张图表；schema chart_count le=3 自我保护，
        # 多余的图表在入口截断（避免调用方手动裁剪 + schema 校验失败）。
        if len(charts) > 3:
            LOGGER.warning(
                "[PdfConverter.convert] charts=%d 超过 3 张，截断到前 3 张",
                len(charts),
            )
            charts = charts[:3]

        try:
            html_str = self._render_html(
                report,
                charts,
                company_name=company_name,
                company_code=company_code,
            )
            # 应用 timeout_seconds（spec §3.7 REPORT 链路 45s SLA；PDF 渲染
            # 预留 30s）。to_thread 把同步阻塞的 WeasyPrint 写入线程化，
            # wait_for 在超时后抛 TimeoutError；底层线程仍可能继续运行
            # （Python asyncio 无法强制取消同步线程），但调用方不会无限等待。
            pdf_bytes = await asyncio.wait_for(
                asyncio.to_thread(self._write_pdf, html_str),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            latency_ms = (time.monotonic() - start) * 1000
            LOGGER.warning(
                "[PdfConverter.convert] WeasyPrint 渲染超时（%ss）",
                self.timeout_seconds,
            )
            return self._fallback(
                report,
                charts,
                latency_ms=latency_ms,
                error=f"WeasyPrint 渲染超时（{self.timeout_seconds}s）: {exc}",
                company_name=company_name,
                company_code=company_code,
            )
        except Exception as exc:  # noqa: BLE001 — 任何异常都降级
            latency_ms = (time.monotonic() - start) * 1000
            LOGGER.warning(
                "[PdfConverter.convert] WeasyPrint 渲染失败: %s",
                exc,
            )
            return self._fallback(
                report,
                charts,
                latency_ms=latency_ms,
                error=f"WeasyPrint 渲染失败: {exc}",
                company_name=company_name,
                company_code=company_code,
            )

        latency_ms = (time.monotonic() - start) * 1000
        chart_count = sum(1 for c in charts if len(c.png_bytes) > 0)
        LOGGER.info(
            "[PdfConverter.convert] success pdf_bytes=%d chart_count=%d latency_ms=%.1f",
            len(pdf_bytes),
            chart_count,
            latency_ms,
        )
        return PdfResult(
            pdf_bytes=pdf_bytes,
            fallback=False,
            error=None,
            chart_count=chart_count,
            page_size=self.page_size,
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _render_html(
        self,
        report: ReportResult,
        charts: list[ChartResult],
        *,
        company_name: str,
        company_code: str,
    ) -> str:
        """渲染 Jinja2 HTML 模板。

        Args:
            report: 5 段式报告。
            charts: 图表列表。
            company_name: 公司名称。
            company_code: 公司股票代码。

        Returns:
            HTML 字符串。
        """
        # 延迟导入 Jinja2 + markdown（避免模块级依赖污染 + 与 chart_renderer
        # 延迟导入模式风格一致）。
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        import markdown as md_lib

        env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=select_autoescape(["html"]),
        )
        template = env.get_template(_TEMPLATE_NAME)

        # 5 段 Markdown → HTML
        sections: list[dict[str, str]] = []
        for section in report.sections:
            html_content = md_lib.markdown(
                section.content,
                extensions=_MARKDOWN_EXTENSIONS,
                output_format="html",
            )
            sections.append(
                {
                    "title": section.title,
                    "html_content": html_content,
                }
            )

        # 图表 PNG → base64 data URL
        chart_dicts: list[dict[str, Any]] = []
        for chart in charts:
            if not chart.png_bytes:
                continue
            png_b64 = base64.b64encode(chart.png_bytes).decode("ascii")
            chart_dicts.append(
                {
                    "title": chart.title,
                    "png_b64": png_b64,
                    "fallback": chart.fallback,
                    "error": chart.error or "",
                }
            )

        # 报告标题：公司名 + 报告期末日
        title_parts: list[str] = []
        if company_name:
            title_parts.append(company_name)
        else:
            title_parts.append("A股财报分析报告")
        title_parts.append("深度解析报告")
        report_title = " ".join(title_parts)

        return template.render(
            report_title=report_title,
            company_name=company_name,
            company_code=company_code,
            report_period=report.report_period,
            fallback=report.fallback,
            page_size=self.page_size,
            sections=sections,
            charts=chart_dicts,
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _write_pdf(self, html_str: str) -> bytes:
        """用 WeasyPrint 把 HTML 转 PDF bytes。

        Args:
            html_str: HTML 字符串。

        Returns:
            PDF 二进制内容。
        """
        # 延迟导入 WeasyPrint（避免模块级 GTK / Pango 依赖加载）。
        from weasyprint import HTML

        buf = io.BytesIO()
        html = HTML(string=html_str)
        html.write_pdf(buf)
        return buf.getvalue()

    def _fallback(
        self,
        report: ReportResult,
        charts: list[ChartResult],
        *,
        latency_ms: float,
        error: str,
        company_name: str,
        company_code: str,
    ) -> PdfResult:
        """生成降级占位 PDF。

        Args:
            report: 原始报告（用于提取标题信息）。
            charts: 原始图表列表（用于 chart_count）。
            latency_ms: 端到端耗时。
            error: 失败原因。
            company_name: 公司名称。
            company_code: 公司股票代码。

        Returns:
            ``PdfResult`` 含最小占位 PDF + ``fallback=True``。
        """
        pdf_bytes = _render_fallback_pdf(
            report=report,
            error=error,
            company_name=company_name,
            company_code=company_code,
        )
        chart_count = sum(1 for c in charts if len(c.png_bytes) > 0)
        return PdfResult(
            pdf_bytes=pdf_bytes,
            fallback=True,
            error=error,
            chart_count=chart_count,
            page_size=self.page_size,
            latency_ms=latency_ms,
        )


# ============================================================================
# 模块级辅助函数
# ============================================================================


def _render_fallback_pdf(
    *,
    report: ReportResult,
    error: str,
    company_name: str,
    company_code: str,
) -> bytes:
    """渲染降级占位 PDF（最小 HTML → PDF）。

    WeasyPrint 失败时调用；若 WeasyPrint 本身不可用则进一步降级为
    纯文本 PDF（用 reportlab 兜底；当前未引入 reportlab，直接返回
    最小 PDF magic bytes 占位）。

    Args:
        report: 原始报告。
        error: 失败原因。
        company_name: 公司名称。
        company_code: 公司股票代码。

    Returns:
        PDF 二进制内容。
    """
    title_parts = []
    if company_name:
        title_parts.append(company_name)
    else:
        title_parts.append("A股财报分析报告")
    title_parts.append("深度解析报告")
    report_title = " ".join(title_parts)

    html_str = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>{report_title} - 降级</title>
  <style>
    @page {{ size: A4; margin: 20mm; }}
    body {{
      font-family: "Noto Sans CJK SC", "Microsoft YaHei", "SimHei", sans-serif;
      font-size: 11pt;
      color: #333;
    }}
    h1 {{ color: #c0392b; font-size: 16pt; }}
    .error {{ background: #fff3cd; padding: 10pt; border: 1pt solid #ffc107;
              border-radius: 3pt; margin: 10pt 0; }}
    .meta {{ color: #666; font-size: 9.5pt; }}
  </style>
</head>
<body>
  <h1>⚠ PDF 渲染失败</h1>
  <div class="meta">
    {report_title}
    {f"· {company_name}" if company_name else ""}
    {f"· {company_code}" if company_code else ""}
    · 报告期末日：{report.report_period}
  </div>
  <div class="error">
    <strong>失败原因：</strong>{error}
  </div>
  <p>5 段 Markdown 报告已生成（{len(report.sections)} 段），但 PDF 转换失败。
  请联系管理员检查 WeasyPrint / 中文字体配置，或访问 Markdown 报告查看完整内容。</p>
  <p>5 段报告标题：</p>
  <ul>
"""
    for section in report.sections:
        html_str += f"    <li>{section.title}</li>\n"
    html_str += "  </ul>\n</body>\n</html>"

    try:
        from weasyprint import HTML

        buf = io.BytesIO()
        HTML(string=html_str).write_pdf(buf)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        LOGGER.error(
            "[PdfConverter._render_fallback_pdf] 占位 PDF 也失败: %s",
            exc,
        )
        # 最终兜底：返回最小合法 PDF（仅含 PDF header，前端展示「PDF 损坏」
        # 但不至于 0 bytes 让 M3.08 L2 上传 MinIO 失败）。
        return _MINIMAL_PDF_PLACEHOLDER


# 最小合法 PDF（11 字节 header + EOF marker），用于 WeasyPrint 完全不可用
# 时的最终兜底。前端打开会显示「PDF 损坏」，但 L2 上传 MinIO 不会因 0
# bytes 失败。
_MINIMAL_PDF_PLACEHOLDER = b"%PDF-1.4\n%%EOF\n"


__all__ = ["PdfConverter"]
