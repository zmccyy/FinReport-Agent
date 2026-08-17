"""M4.06 report handler — 只读 MySQL 拉数据 + 报告/图表/PDF 真实生成.

数据链路（decision record #6：check/report 数据从 MySQL 读，L3 只读）：

1. ``ReadOnlyMySqlClient.fetch_report_statements`` 按 taskId 拉三表科目行；
2. ``fetch_check_result`` 回读 CHECK 持久化的勾稽 + 异常结果（保留 LLM
   复核 note，避免重复调 API）；
3. ``ReportGenerator.generate`` 走 DeepSeek 生成 5 段 Markdown 报告
   （内建降级：API 失败走模板，5 段齐全）；
4. ``ChartRenderer.render_all`` 渲染 3 张 PNG 图表（to_thread 包同步
   matplotlib，避免阻塞事件循环；内建占位图降级）；
5. ``PdfConverter.convert`` Markdown + 图表 → PDF（内建占位 PDF 降级）；
6. 组装 M3.08 契约 payload（markdown / pdf_b64 / charts[].png_b64），
   L2 ``ReportArtifactWriter`` 上传 MinIO 并写 report_artifact 表。

失败语义：DB 读失败 / 科目行缺失 / 勾稽结果缺失均抛 ``AiException``
（上游步骤未落库，REPORT 无法进行，DLQ 保留现场）；LLM / 图表 / PDF
环节自身降级，payload ``fallback`` 字段聚合降级标记。
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AiException
from app.core.mysql_client import (
    ReadOnlyMySqlClient,
    ReportStatements,
    StatementReader,
    StatementRow,
)
from app.modules.generator.chart_renderer import ChartRenderer
from app.modules.generator.pdf_converter import PdfConverter
from app.modules.generator.report_generator import ReportGenerator
from app.modules.modelhub.modelhub import ModelHub, get_modelhub
from app.schemas.chart import ChartResult
from app.schemas.pdf import PdfResult
from app.schemas.report import ReportResult
from app.schemas.statement import FinancialStatement, StatementItem, StatementType
from app.schemas.task import TaskMessage
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# 科目行筛选偏好（与 reasoner handler 一致：合并口径 + 本期优先）。
_PREFERRED_SCOPE = "合并"
_PREFERRED_PERIOD = "本期"

_reader: StatementReader | None = None
_generator: ReportGenerator | None = None
_chart_renderer: ChartRenderer | None = None
_pdf_converter: PdfConverter | None = None


class _JsonModeHub:
    """ModelHub 适配器：报告生成场景注入 json_mode（AGENTS.md §8.1 同理）。

    报告 prompt 输出 ``{"sections": [...]}`` JSON，与抽取/复核同理开启
    ``response_format=json_object`` 提升解析成功率；在适配层注入而不改
    ``ReportGenerator``，保持其既有调用契约不变。
    """

    def __init__(self, hub: ModelHub) -> None:
        self._hub = hub
        # ReportGenerator 要求 hub.settings 属性。
        self.settings = hub.settings

    def generate(self, prompt: str, **kwargs: Any) -> Any:
        """Delegate to the wrapped hub, forcing json_mode on."""
        kwargs.setdefault("json_mode", True)
        return self._hub.generate(prompt, **kwargs)


def configure_handler(
    *,
    reader: StatementReader | None = None,
    generator: ReportGenerator | None = None,
    chart_renderer: ChartRenderer | None = None,
    pdf_converter: PdfConverter | None = None,
) -> None:
    """Inject reader/generator/chart/pdf dependencies (used by unit tests).

    Args:
        reader: Optional StatementReader override.
        generator: Optional ReportGenerator override.
        chart_renderer: Optional ChartRenderer override.
        pdf_converter: Optional PdfConverter override.
    """
    global _reader, _generator, _chart_renderer, _pdf_converter
    _reader = reader
    _generator = generator
    _chart_renderer = chart_renderer
    _pdf_converter = pdf_converter


def reset_handler() -> None:
    """Clear injected dependencies so defaults are rebuilt lazily."""
    configure_handler(
        reader=None,
        generator=None,
        chart_renderer=None,
        pdf_converter=None,
    )


def _resolve_reader() -> StatementReader:
    """Return the configured reader or build the production default."""
    return _reader if _reader is not None else ReadOnlyMySqlClient(Settings())


def _resolve_generator() -> ReportGenerator:
    """Return the configured generator or build the production default."""
    return _generator if _generator is not None else ReportGenerator(_JsonModeHub(get_modelhub()))


def _resolve_chart_renderer() -> ChartRenderer:
    """Return the configured chart renderer or build the production default."""
    return _chart_renderer if _chart_renderer is not None else ChartRenderer()


def _resolve_pdf_converter() -> PdfConverter:
    """Return the configured pdf converter or build the production default."""
    return _pdf_converter if _pdf_converter is not None else PdfConverter()


def build_financial_statement(data: ReportStatements) -> FinancialStatement:
    """把 DB 科目行组装成 ``FinancialStatement``（合并 + 本期优先）。

    筛选策略与 reasoner handler 的 ``build_snapshot`` 一致：每个表类型先按
    period_type=本期 过滤（无则保留全部），再按 scope=合并 过滤；防止
    合并/母公司两套同名科目互相覆盖。

    Args:
        data: MySQL 查询结果。

    Returns:
        ReportGenerator / ChartRenderer 消费的 ``FinancialStatement``。
    """
    by_type: dict[str, list[StatementRow]] = {}
    for row in data.rows:
        by_type.setdefault(row.statement_type, []).append(row)

    statements: dict[StatementType, list[StatementItem]] = {}
    for type_value, rows in by_type.items():
        try:
            st_type = StatementType(type_value)
        except ValueError:
            LOGGER.warning(
                "[build_financial_statement] 未知 statement_type=%s，跳过",
                type_value,
            )
            continue
        picked = _prefer(rows, lambda r: r.period_type == _PREFERRED_PERIOD)
        picked = _prefer(picked, lambda r: r.scope == _PREFERRED_SCOPE)
        items = [
            StatementItem(item=row.item_name, value=float(row.item_value))
            for row in picked
            if row.item_value is not None
        ]
        if items:
            statements[st_type] = items
    return FinancialStatement(
        report_period=data.report_period,
        currency=data.currency,
        unit=data.unit,
        statements=statements,
    )


def _prefer(rows: list[StatementRow], predicate: Any) -> list[StatementRow]:
    """命中谓词的行非空时只保留命中行，否则原样返回。"""
    picked = [row for row in rows if predicate(row)]
    return picked if picked else rows


async def handle(message: TaskMessage) -> dict[str, Any]:
    """生成 5 段报告 + 3 张图表 + PDF，组装 M3.08 契约 payload。

    Args:
        message: Validated report task message（step 必须为 ``report``）。

    Returns:
        M3.08 契约 payload（L2 ``ReportArtifactWriter`` 消费）：
        ``report_period / markdown / pdf_b64 / charts / fallback``。

    Raises:
        ValueError: When the routing step is not ``report``.
        AiException: When DB read fails or upstream data is missing.
    """
    if message.step != "report":
        raise ValueError(f"Unknown generator step: {message.step!r}, expected 'report'")

    reader = _resolve_reader()
    data = reader.fetch_report_statements(message.task_id)
    if not data.rows:
        raise AiException(
            f"no financial_statement rows for taskId={message.task_id} "
            f"reportId={data.report_id}（EXTRACT 步骤未写入或失败）"
        )
    check_result = reader.fetch_check_result(data.report_id)
    if check_result is None:
        raise AiException(
            f"no accounting_check/anomaly rows for taskId={message.task_id} "
            f"reportId={data.report_id}（CHECK 步骤未写入或失败）"
        )

    statement = build_financial_statement(data)

    # 1. NLG 报告（DeepSeek；内建模板降级，API 失败不抛）。
    report_result = await _resolve_generator().generate(
        statement,
        check_result,
        company_name=data.company_name,
        company_code=data.company_code,
    )

    # 2. 图表渲染（matplotlib 同步阻塞，to_thread 让出事件循环）。
    charts = await asyncio.to_thread(_resolve_chart_renderer().render_all, statement)

    # 3. Markdown + 图表 → PDF（WeasyPrint；内建占位 PDF 降级）。
    pdf_result = await _resolve_pdf_converter().convert(
        report_result,
        charts,
        company_name=data.company_name,
        company_code=data.company_code,
    )

    LOGGER.info(
        "[handle] report 完成 taskId=%s reportId=%s fallback_report=%s "
        "charts=%d pdf_fallback=%s tokens=%d latency_ms=%.1f",
        message.task_id,
        data.report_id,
        report_result.fallback,
        len(charts),
        pdf_result.fallback,
        report_result.prompt_tokens + report_result.completion_tokens,
        report_result.latency_ms + pdf_result.latency_ms,
    )
    return _build_payload(report_result, charts, pdf_result)


def _build_payload(
    report: ReportResult,
    charts: list[ChartResult],
    pdf: PdfResult,
) -> dict[str, Any]:
    """组装 M3.08 契约 payload（L2 ReportArtifactWriter 消费）。

    Args:
        report: 报告生成结果。
        charts: 图表渲染结果列表。
        pdf: PDF 转换结果。

    Returns:
        含 ``markdown`` / ``pdf_b64`` / ``charts[].png_b64`` 的 payload；
        ``fallback`` 聚合报告与 PDF 两侧降级标记（图表降级不改变整体
        标记——占位图自身可展示）。
    """
    return {
        "report_period": report.report_period,
        "markdown": report.to_markdown(),
        "pdf_b64": base64.b64encode(pdf.pdf_bytes).decode("ascii"),
        "charts": [
            {
                "chart_type": chart.chart_type.value,
                "title": chart.title,
                "png_b64": base64.b64encode(chart.png_bytes).decode("ascii"),
            }
            for chart in charts
        ],
        "fallback": report.fallback or pdf.fallback,
    }
