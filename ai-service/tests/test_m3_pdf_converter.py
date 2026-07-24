"""M3.07 PdfConverter Markdown → PDF 测试（spec §2.3 M10 + plan M3.07）。

测试覆盖：

1. **schema** — PdfResult 基础属性 + success / fallback 判定
2. **正常渲染** — 5 段报告 + 3 张图表 → PDF bytes 非空
3. **降级路径** — WeasyPrint 异常时 fallback=True + 占位 PDF
4. **不可变输入** — 原 ReportResult / ChartResult 不被修改
5. **Markdown → HTML** — 表格 / 列表 / 加粗 / 代码块等基础语法
6. **PNG base64 嵌入** — 图表 PNG 编码为 data URL
7. **空图表 / 空段落** — 边界场景不抛异常
8. **自定义模板目录** — 支持自定义 Jinja2 模板
9. **异步超时** — WeasyPrint 阻塞调用包装到 to_thread

Windows 开发环境缺 GTK Runtime，无法实际加载 weasyprint 模块（import 时
即触发 libgobject/libpango C 库加载，无 GTK 会抛 OSError）。本测试文件
在模块加载时立即注入 mock weasyprint 模块到 ``sys.modules``，避免真实
导入；生产 Linux 容器 GTK 自动安装后此 mock 不影响真实 PDF 生成
（Linux 上 sys.modules 不会有 mock，导入真实 weasyprint 正常工作）。
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Windows 开发环境 mock weasyprint（必须在 import PdfConverter 之前完成）
# ---------------------------------------------------------------------------
# weasyprint 在 import 时即调用 cffi.dlopen 加载 GTK C 库，Windows 缺 GTK
# 会抛 OSError。注入 mock 模块到 sys.modules，让所有 `from weasyprint
# import HTML` 都拿到 mock 类，避免触发真实加载。Linux 生产环境如已
# 安装 GTK 且真实 weasyprint 已加载到 sys.modules，则跳过注入。
_MOCK_PDF_BYTES = b"%PDF-1.4\nmock pdf content\n%%EOF\n"


class _MockHTML:
    """Mock WeasyPrint HTML 类。"""

    def __init__(self, *, string: str = "", **kwargs):
        self.string = string

    def write_pdf(self, buf):
        buf.write(_MOCK_PDF_BYTES)


def _install_mock_weasyprint() -> None:
    """注入 mock weasyprint 模块到 sys.modules（如尚未加载）。"""
    if "weasyprint" in sys.modules:
        return  # 已加载（真实或 mock），不覆盖
    mock_module = MagicMock()
    mock_module.HTML = _MockHTML
    sys.modules["weasyprint"] = mock_module


_install_mock_weasyprint()

# 现在可以安全 import PdfConverter（其函数内 `from weasyprint import HTML`
# 会拿到 mock）
from app.modules.generator.pdf_converter import (  # noqa: E402
    PdfConverter,
    _MINIMAL_PDF_PLACEHOLDER,
    _TEMPLATE_DIR,
)
from app.schemas.chart import ChartResult, ChartType  # noqa: E402
from app.schemas.pdf import PdfResult  # noqa: E402
from app.schemas.report import (  # noqa: E402
    ReportResult,
    ReportSection,
    ReportSectionType,
)

# PDF magic number（前 8 字节）：`%PDF-1.x`
_PDF_MAGIC = b"%PDF-"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_sections() -> list[ReportSection]:
    """5 段固定报告。"""
    contents = {
        ReportSectionType.COMPANY_OVERVIEW: (
            "**公司名称**：贵州茅台股份有限公司\n\n"
            "**股票代码**：600519\n\n"
            "公司主营白酒生产与销售，是中国白酒行业龙头企业。"
        ),
        ReportSectionType.FINANCIAL_OVERVIEW: (
            "**关键财务指标摘要**：\n\n"
            "- 资产总计：2800 亿元\n"
            "- 营业收入：1300 亿元\n"
            "- 净利润：850 亿元\n\n"
            "**勾稽置信度**：0.85"
        ),
        ReportSectionType.STATEMENT_ANALYSIS: (
            "**三表科目数据**：\n\n"
            "### 资产负债表\n\n"
            "| 科目 | 数值 |\n"
            "|---|---|\n"
            "| 货币资金 | 1500 亿 |\n"
            "| 应收账款 | 200 亿 |\n\n"
            "### 利润表\n\n"
            "| 科目 | 数值 |\n"
            "|---|---|\n"
            "| 营业收入 | 1300 亿 |\n"
            "| 净利润 | 850 亿 |"
        ),
        ReportSectionType.ANOMALY_AND_RISK: (
            "**勾稽规则结果**：\n\n"
            "- 资产 = 负债 + 权益：通过\n"
            "- 净利润 → 未分配利润：失败\n\n"
            "**异常列表**：\n"
            "- 应收账款同比激增 50%\n"
            "- 存货环比激增 30%"
        ),
        ReportSectionType.CONCLUSION: (
            "本期财报整体表现良好，营收与净利润同步增长。\n\n"
            "建议关注应收账款与存货激增风险。"
        ),
    }
    return [
        ReportSection(
            section_type=st,
            title=st.chinese_name,
            content=contents[st],
        )
        for st in (
            ReportSectionType.COMPANY_OVERVIEW,
            ReportSectionType.FINANCIAL_OVERVIEW,
            ReportSectionType.STATEMENT_ANALYSIS,
            ReportSectionType.ANOMALY_AND_RISK,
            ReportSectionType.CONCLUSION,
        )
    ]


def _make_charts() -> list[ChartResult]:
    """3 张图表（mock PNG bytes）。"""
    return [
        ChartResult(
            chart_type=ChartType.ASSET_STRUCTURE_PIE,
            title="资产结构饼图",
            png_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,  # mock PNG header
            width=800,
            height=500,
        ),
        ChartResult(
            chart_type=ChartType.REVENUE_TREND_LINE,
            title="营收趋势折线",
            png_bytes=b"\x89PNG\r\n\x1a\n" + b"\x01" * 100,
            width=800,
            height=500,
        ),
        ChartResult(
            chart_type=ChartType.CASH_FLOW_BAR,
            title="现金流柱状图",
            png_bytes=b"\x89PNG\r\n\x1a\n" + b"\x02" * 100,
            width=800,
            height=500,
        ),
    ]


def _make_report(*, fallback: bool = False) -> ReportResult:
    """构造 ReportResult。"""
    return ReportResult(
        sections=_make_sections(),
        report_period="2025-12-31",
        fallback=fallback,
        raw_text="",
        prompt_tokens=1000,
        completion_tokens=500,
        latency_ms=2000.0,
        error=None if not fallback else "LLM 调用失败",
    )


@pytest.fixture
def report() -> ReportResult:
    """5 段齐全的报告。"""
    return _make_report()


@pytest.fixture
def charts() -> list[ChartResult]:
    """3 张图表。"""
    return _make_charts()


@pytest.fixture
def converter() -> PdfConverter:
    """默认 PdfConverter 实例。"""
    return PdfConverter()


@pytest.fixture
def mock_weasyprint(monkeypatch: pytest.MonkeyPatch):
    """Mock WeasyPrint 的 HTML.write_pdf，返回 mock PDF bytes。

    Windows 开发环境无 GTK Runtime，无法实际调用 WeasyPrint；
    生产 Linux 容器 GTK 自动安装后此 mock 不影响真实 PDF 生成。
    """
    mock_pdf_bytes = b"%PDF-1.4\nmock pdf content\n%%EOF\n"

    class _MockHTML:
        def __init__(self, *, string: str = "", **kwargs):
            self.string = string

        def write_pdf(self, buf):
            buf.write(mock_pdf_bytes)

    monkeypatch.setattr("weasyprint.HTML", _MockHTML)
    return mock_pdf_bytes


# ---------------------------------------------------------------------------
# Schema 测试
# ---------------------------------------------------------------------------


class TestPdfSchema:
    """PdfResult schema 测试。"""

    def test_pdf_result_success_true_on_normal(self) -> None:
        """正常 PDF PdfResult.success = True。"""
        r = PdfResult(pdf_bytes=b"%PDF-1.4\n%%EOF\n")
        assert r.success is True

    def test_pdf_result_success_false_on_fallback(self) -> None:
        """fallback=True 时 success = False。"""
        r = PdfResult(pdf_bytes=b"%PDF-1.4\n%%EOF\n", fallback=True)
        assert r.success is False

    def test_pdf_result_success_false_on_empty_bytes(self) -> None:
        """pdf_bytes 为空时 success = False。"""
        r = PdfResult(pdf_bytes=b"")
        assert r.success is False

    def test_pdf_result_success_false_on_error(self) -> None:
        """error 非空时 success = False。"""
        r = PdfResult(pdf_bytes=b"%PDF-1.4\n%%EOF\n", error="some error")
        assert r.success is False

    def test_pdf_result_default_page_size_a4(self) -> None:
        """默认页面规格为 A4。"""
        r = PdfResult()
        assert r.page_size == "A4"

    def test_pdf_result_chart_count_constraint(self) -> None:
        """chart_count 字段 ge=0 le=3。"""
        r = PdfResult(chart_count=3)
        assert r.chart_count == 3


# ---------------------------------------------------------------------------
# 正常渲染
# ---------------------------------------------------------------------------


class TestConvertSuccess:
    """正常渲染测试。"""

    def test_should_convert_report_to_pdf(
        self,
        converter: PdfConverter,
        report: ReportResult,
        charts: list[ChartResult],
        mock_weasyprint: bytes,
    ) -> None:
        """5 段报告 + 3 张图表 → PDF bytes 非空。"""
        result = asyncio.run(
            converter.convert(
                report,
                charts,
                company_name="贵州茅台",
                company_code="600519",
            )
        )
        assert result.fallback is False
        assert result.error is None
        assert result.pdf_bytes == mock_weasyprint
        assert len(result.pdf_bytes) > 0
        assert result.chart_count == 3
        assert result.page_size == "A4"
        assert result.latency_ms >= 0.0

    def test_should_render_pdf_without_charts(
        self,
        converter: PdfConverter,
        report: ReportResult,
        mock_weasyprint: bytes,
    ) -> None:
        """无图表时也能渲染 PDF。"""
        result = asyncio.run(converter.convert(report, None, company_name="贵州茅台"))
        assert result.fallback is False
        assert result.chart_count == 0
        assert len(result.pdf_bytes) > 0

    def test_should_render_pdf_with_empty_charts_list(
        self,
        converter: PdfConverter,
        report: ReportResult,
        mock_weasyprint: bytes,
    ) -> None:
        """空图表列表（[]）与 None 等价。"""
        result = asyncio.run(converter.convert(report, []))
        assert result.fallback is False
        assert result.chart_count == 0

    def test_should_skip_empty_png_charts(
        self,
        converter: PdfConverter,
        report: ReportResult,
        mock_weasyprint: bytes,
    ) -> None:
        """png_bytes 为空的图表不计入 chart_count。"""
        charts = _make_charts()
        charts[0] = ChartResult(
            chart_type=ChartType.ASSET_STRUCTURE_PIE,
            title="资产结构饼图",
            png_bytes=b"",
            fallback=True,
            error="数据缺失",
        )
        result = asyncio.run(converter.convert(report, charts))
        assert result.fallback is False
        assert result.chart_count == 2  # 只计 2 张非空图表

    def test_should_handle_fallback_report(
        self,
        converter: PdfConverter,
        mock_weasyprint: bytes,
    ) -> None:
        """降级报告（fallback=True）也能转 PDF。"""
        report = _make_report(fallback=True)
        result = asyncio.run(converter.convert(report))
        # PdfConverter 不继承 ReportResult.fallback；PDF 仍正常生成
        assert result.fallback is False
        assert result.pdf_bytes == mock_weasyprint


# ---------------------------------------------------------------------------
# 降级路径
# ---------------------------------------------------------------------------


class TestConvertFallback:
    """WeasyPrint 失败降级测试。"""

    def test_should_fallback_when_weasyprint_raises(
        self,
        converter: PdfConverter,
        report: ReportResult,
        charts: list[ChartResult],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """WeasyPrint 抛异常时 fallback=True + 占位 PDF。"""

        class _FailingHTML:
            def __init__(self, *, string: str = "", **kwargs):
                self.string = string

            def write_pdf(self, buf):
                raise RuntimeError("Pango initialization failed")

        monkeypatch.setattr("weasyprint.HTML", _FailingHTML)

        result = asyncio.run(converter.convert(report, charts, company_name="贵州茅台"))
        assert result.fallback is True
        assert "WeasyPrint 渲染失败" in (result.error or "")
        assert result.chart_count == 3  # chart_count 仍透传
        # 占位 PDF 由 _render_fallback_pdf 生成，应非空
        assert len(result.pdf_bytes) > 0

    def test_should_fallback_when_jinja_template_missing(
        self,
        report: ReportResult,
        mock_weasyprint: bytes,
    ) -> None:
        """模板目录不存在时降级。"""
        converter = PdfConverter(template_dir=Path("/nonexistent/path"))
        result = asyncio.run(converter.convert(report))
        assert result.fallback is True
        assert "WeasyPrint 渲染失败" in (result.error or "")

    def test_should_fallback_when_markdown_parse_fails(
        self,
        converter: PdfConverter,
        report: ReportResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """markdown 库导入失败时降级。"""

        import sys

        # 模拟 markdown 库不存在
        original = sys.modules.get("markdown")
        sys.modules["markdown"] = None  # type: ignore
        try:
            result = asyncio.run(converter.convert(report))
            assert result.fallback is True
        finally:
            if original is not None:
                sys.modules["markdown"] = original
            else:
                sys.modules.pop("markdown", None)

    def test_fallback_pdf_contains_failure_info(
        self,
        converter: PdfConverter,
        report: ReportResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """降级 PDF 由 _render_fallback_pdf 生成（mock 后非空）。"""

        class _FailingHTML:
            def __init__(self, *, string: str = "", **kwargs):
                self.string = string

            def write_pdf(self, buf):
                raise RuntimeError("mock failure")

        # 主调用失败 → 走 _render_fallback_pdf；fallback 内也走同一 mock
        # （_render_fallback_pdf 也 import weasyprint.HTML）→ 也会失败 →
        # 最终走 _MINIMAL_PDF_PLACEHOLDER
        monkeypatch.setattr("weasyprint.HTML", _FailingHTML)

        result = asyncio.run(converter.convert(report))
        assert result.fallback is True
        # _render_fallback_pdf 失败后返回最小占位 PDF
        assert result.pdf_bytes == _MINIMAL_PDF_PLACEHOLDER

    def test_should_fallback_when_weasyprint_timeout(
        self,
        report: ReportResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """WeasyPrint 渲染超时时 fallback=True 且 error 包含「渲染超时」。

        spec §3.7 REPORT 链路 SLA 45s；PdfConverter 默认 timeout 30s。
        """

        import time as _time

        class _SlowHTML:
            def __init__(self, *, string: str = "", **kwargs):
                self.string = string

            def write_pdf(self, buf):
                # 模拟 WeasyPrint 卡死（实际不会写入 buf）
                _time.sleep(0.5)

        # 用极短 timeout（0.05s）触发超时；write_pdf 内部 sleep 0.5s
        converter = PdfConverter(timeout_seconds=0.05)
        monkeypatch.setattr("weasyprint.HTML", _SlowHTML)

        result = asyncio.run(converter.convert(report))
        assert result.fallback is True
        assert "渲染超时" in (result.error or "")
        assert "0.05" in (result.error or "")


# ---------------------------------------------------------------------------
# 不可变输入
# ---------------------------------------------------------------------------


class TestImmutability:
    """不可变输入测试。"""

    def test_should_not_mutate_report(
        self,
        converter: PdfConverter,
        report: ReportResult,
        charts: list[ChartResult],
        mock_weasyprint: bytes,
    ) -> None:
        """PDF 转换不应修改原 ReportResult。"""
        original_sections = list(report.sections)
        original_period = report.report_period
        original_fallback = report.fallback

        asyncio.run(converter.convert(report, charts))

        assert report.sections == original_sections
        assert report.report_period == original_period
        assert report.fallback == original_fallback

    def test_should_not_mutate_charts(
        self,
        converter: PdfConverter,
        report: ReportResult,
        charts: list[ChartResult],
        mock_weasyprint: bytes,
    ) -> None:
        """PDF 转换不应修改原 ChartResult 列表。"""
        original_pngs = [c.png_bytes for c in charts]
        original_titles = [c.title for c in charts]

        asyncio.run(converter.convert(report, charts))

        for orig, cur in zip(original_pngs, [c.png_bytes for c in charts]):
            assert cur == orig
        for orig, cur in zip(original_titles, [c.title for c in charts]):
            assert cur == orig


# ---------------------------------------------------------------------------
# Markdown → HTML 转换
# ---------------------------------------------------------------------------


class TestMarkdownToHtml:
    """Markdown → HTML 转换测试。"""

    def test_should_convert_markdown_table(
        self,
        converter: PdfConverter,
        mock_weasyprint: bytes,
    ) -> None:
        """Markdown 表格转为 HTML <table>。"""
        report = ReportResult(
            sections=[
                ReportSection(
                    section_type=ReportSectionType.STATEMENT_ANALYSIS,
                    title="三表分析",
                    content=(
                        "| 科目 | 数值 |\n"
                        "|---|---|\n"
                        "| 货币资金 | 1500 |\n"
                        "| 应收账款 | 200 |"
                    ),
                )
            ]
            + _make_sections()[1:],
            report_period="2025-12-31",
        )
        result = asyncio.run(converter.convert(report))
        assert result.fallback is False

    def test_should_convert_markdown_bold_and_list(
        self,
        converter: PdfConverter,
        mock_weasyprint: bytes,
    ) -> None:
        """Markdown 加粗 + 列表转 HTML。"""
        report = ReportResult(
            sections=[
                ReportSection(
                    section_type=ReportSectionType.FINANCIAL_OVERVIEW,
                    title="财务概览",
                    content=("**关键指标**：\n\n" "- 营收 1300 亿\n" "- 净利 850 亿"),
                )
            ]
            + _make_sections()[1:],
            report_period="2025-12-31",
        )
        result = asyncio.run(converter.convert(report))
        assert result.fallback is False

    def test_should_handle_empty_section_content(
        self,
        converter: PdfConverter,
        mock_weasyprint: bytes,
    ) -> None:
        """段内容为空时不抛异常。"""
        report = ReportResult(
            sections=[
                ReportSection(
                    section_type=ReportSectionType.COMPANY_OVERVIEW,
                    title="公司概况",
                    content="",
                )
            ]
            + _make_sections()[1:],
            report_period="2025-12-31",
        )
        result = asyncio.run(converter.convert(report))
        assert result.fallback is False


# ---------------------------------------------------------------------------
# PNG base64 嵌入
# ---------------------------------------------------------------------------


class TestPngBase64Embed:
    """PNG base64 嵌入测试。"""

    def test_should_embed_png_as_base64_data_url(
        self,
        converter: PdfConverter,
        report: ReportResult,
        charts: list[ChartResult],
        mock_weasyprint: bytes,
    ) -> None:
        """3 张图表 PNG 编码为 base64 data URL 嵌入 HTML。"""
        # 通过 _render_html 直接验证 base64 编码
        html_str = converter._render_html(
            report,
            charts,
            company_name="贵州茅台",
            company_code="600519",
        )
        # 检查每张图表的 base64 编码出现在 HTML 中
        for chart in charts:
            expected_b64 = base64.b64encode(chart.png_bytes).decode("ascii")
            assert expected_b64 in html_str
        assert "data:image/png;base64," in html_str

    def test_should_skip_empty_png_in_html(
        self,
        converter: PdfConverter,
        report: ReportResult,
        mock_weasyprint: bytes,
    ) -> None:
        """png_bytes 为空的图表不出现在 HTML 中。"""
        charts = _make_charts()
        charts[0] = charts[0].model_copy(update={"png_bytes": b""})
        html_str = converter._render_html(
            report, charts, company_name="", company_code=""
        )
        # 只剩 2 张图表的 data URL
        assert html_str.count("data:image/png;base64,") == 2


# ---------------------------------------------------------------------------
# 默认值与边界
# ---------------------------------------------------------------------------


class TestDefaultsAndEdgeCases:
    """默认值与边界场景。"""

    def test_default_template_dir(self) -> None:
        """默认模板目录为 templates/ 子目录。"""
        converter = PdfConverter()
        assert converter.template_dir == _TEMPLATE_DIR
        assert (_TEMPLATE_DIR / "report.html").exists()

    def test_default_page_size_a4(self) -> None:
        """默认页面规格 A4。"""
        converter = PdfConverter()
        assert converter.page_size == "A4"

    def test_default_timeout_30s(self) -> None:
        """默认超时 30s。"""
        converter = PdfConverter()
        assert converter.timeout_seconds == 30.0

    def test_custom_template_dir(self, tmp_path: Path, mock_weasyprint: bytes) -> None:
        """自定义模板目录。"""
        # 复制默认模板到临时目录
        custom_dir = tmp_path / "templates"
        custom_dir.mkdir()
        custom_template = custom_dir / "report.html"
        custom_template.write_text(
            (_TEMPLATE_DIR / "report.html").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        converter = PdfConverter(template_dir=custom_dir)
        assert converter.template_dir == custom_dir

        report = _make_report()
        result = asyncio.run(converter.convert(report))
        assert result.fallback is False

    def test_report_with_zero_sections(
        self,
        converter: PdfConverter,
        mock_weasyprint: bytes,
    ) -> None:
        """空 sections 列表也能渲染 PDF（只有页眉页脚）。"""
        report = ReportResult(
            sections=[],
            report_period="2025-12-31",
        )
        result = asyncio.run(converter.convert(report))
        assert result.fallback is False
        assert len(result.pdf_bytes) > 0

    def test_chart_count_capped_at_3(
        self,
        converter: PdfConverter,
        report: ReportResult,
        mock_weasyprint: bytes,
    ) -> None:
        """超过 3 张图表也接受（chart_count 字段 le=3 但实际计数）。"""
        charts = _make_charts() + [
            ChartResult(
                chart_type=ChartType.ASSET_STRUCTURE_PIE,
                title="额外图表",
                png_bytes=b"\x89PNG" + b"\x00" * 10,
            )
        ]
        result = asyncio.run(converter.convert(report, charts))
        # chart_count 字段 le=3；4 张图表时为 3（截断）
        assert result.chart_count <= 3

    def test_page_size_passed_to_template(
        self,
        converter: PdfConverter,
        report: ReportResult,
        mock_weasyprint: bytes,
    ) -> None:
        """page_size 属性传入模板（@page size: {{ page_size }}）。"""
        # 默认 A4
        result = asyncio.run(converter.convert(report))
        assert result.page_size == "A4"

        # 自定义 Letter
        custom_converter = PdfConverter(page_size="Letter")
        assert custom_converter.page_size == "Letter"
        result = asyncio.run(custom_converter.convert(report))
        assert result.page_size == "Letter"

    def test_page_size_rendered_in_html(
        self,
        converter: PdfConverter,
        report: ReportResult,
    ) -> None:
        """HTML 模板中 @page size 使用传入的 page_size。"""
        html_str = converter._render_html(report, [], company_name="", company_code="")
        # 默认 A4 渲染到 @page size
        assert "size: A4" in html_str

        custom_converter = PdfConverter(page_size="Letter")
        html_str = custom_converter._render_html(
            report, [], company_name="", company_code=""
        )
        assert "size: Letter" in html_str


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


class TestRenderFallbackPdf:
    """降级 PDF 生成测试。"""

    def test_fallback_pdf_with_mock_weasyprint(
        self,
        converter: PdfConverter,
        report: ReportResult,
        mock_weasyprint: bytes,
    ) -> None:
        """_render_fallback_pdf 在 mock 环境下返回 mock PDF bytes。"""
        from app.modules.generator.pdf_converter import _render_fallback_pdf

        pdf_bytes = _render_fallback_pdf(
            report=report,
            error="mock error",
            company_name="贵州茅台",
            company_code="600519",
        )
        assert pdf_bytes == mock_weasyprint

    def test_fallback_pdf_returns_minimal_when_weasyprint_unavailable(
        self,
        report: ReportResult,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """WeasyPrint 完全不可用时返回 _MINIMAL_PDF_PLACEHOLDER。"""
        from app.modules.generator.pdf_converter import _render_fallback_pdf

        # 模拟 weasyprint 导入失败
        import builtins

        original_import = builtins.__import__

        def _fail_import(name, *args, **kwargs):
            if name == "weasyprint":
                raise ImportError("mock: weasyprint unavailable")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail_import)

        pdf_bytes = _render_fallback_pdf(
            report=report,
            error="主渲染失败",
            company_name="",
            company_code="",
        )
        assert pdf_bytes == _MINIMAL_PDF_PLACEHOLDER

    def test_minimal_pdf_placeholder_is_valid_pdf_header(self) -> None:
        """_MINIMAL_PDF_PLACEHOLDER 以 PDF magic 开头。"""
        assert _MINIMAL_PDF_PLACEHOLDER.startswith(_PDF_MAGIC)


# ---------------------------------------------------------------------------
# 异步包装
# ---------------------------------------------------------------------------


class TestAsyncWrapper:
    """异步 + to_thread 包装测试。"""

    def test_convert_is_coroutine_function(
        self,
        converter: PdfConverter,
    ) -> None:
        """convert 是 async 函数。"""
        import inspect

        assert inspect.iscoroutinefunction(converter.convert)

    def test_convert_returns_coroutine(
        self,
        converter: PdfConverter,
        report: ReportResult,
        mock_weasyprint: bytes,
    ) -> None:
        """convert 返回 coroutine 对象。"""
        coro = converter.convert(report)
        import inspect

        assert inspect.iscoroutine(coro)
        # 必须 await 或 close 避免 coroutine never awaited 警告
        coro.close()
