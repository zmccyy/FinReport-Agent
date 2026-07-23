"""M10 报告生成模块（spec §2.3 M10 + plan M3.05+）。

子模块：

* ``prompts`` — 5 段式报告 chat-style prompt 构建。
* ``report_generator`` — 异步 ``ReportGenerator`` 类（NLG 入口）。
* ``chart_renderer`` — ``ChartRenderer`` 类（3 张 PNG 图表服务端渲染）。
* ``pdf_converter`` — ``PdfConverter`` 类（Markdown + 图表 → PDF）。
"""

from app.modules.generator.chart_renderer import ChartRenderer
from app.modules.generator.pdf_converter import PdfConverter
from app.modules.generator.prompts import build_report_prompt
from app.modules.generator.report_generator import ReportGenerator

__all__ = [
    "ChartRenderer",
    "PdfConverter",
    "ReportGenerator",
    "build_report_prompt",
]
