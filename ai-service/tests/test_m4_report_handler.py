"""M4.06 report handler 单测：报告/图表/PDF 真实生成 + M3.08 契约 payload."""

from __future__ import annotations

import asyncio
import base64
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import AiException
from app.core.mysql_client import ReportStatements, StatementRow
from app.modules.generator.handler import (
    _JsonModeHub,
    build_financial_statement,
    configure_handler,
    handle,
    reset_handler,
)
from app.schemas.chart import ChartResult, ChartType
from app.schemas.pdf import PdfResult
from app.schemas.reasoning import CheckResult, RuleResult, Severity
from app.schemas.report import (
    ReportResult,
    ReportSection,
    ReportSectionType,
)
from app.schemas.statement import StatementType
from app.schemas.task import TaskMessage

# ---------------------------------------------------------------------------
# Fake 基础设施
# ---------------------------------------------------------------------------


def make_row(
    statement_type: str,
    item_name: str,
    value: Decimal | float,
    *,
    scope: str = "合并",
    period_type: str = "本期",
) -> StatementRow:
    """Build one financial_statement row."""
    return StatementRow(
        statement_type=statement_type,
        item_name=item_name,
        item_value=Decimal(str(value)),
        scope=scope,
        period_type=period_type,
    )


def make_data(*, task_id: str = "task-r1") -> ReportStatements:
    """Build a complete three-statement dataset."""
    return ReportStatements(
        report_id=9,
        company_code="600000",
        company_name="浦发银行",
        report_period="2024-12-31",
        currency="CNY",
        unit="元",
        rows=(
            make_row("balance_sheet", "资产总计", 300),
            make_row("balance_sheet", "负债合计", 200),
            make_row("balance_sheet", "所有者权益合计", 100),
            make_row("income_statement", "营业收入", 80),
            make_row("income_statement", "净利润", 12),
            make_row("cash_flow", "经营活动产生的现金流量净额", 20),
        ),
    )


def make_check_result() -> CheckResult:
    """Build a persisted-check-result stand-in (rules + one anomaly)."""
    return CheckResult(
        rules=[
            RuleResult(
                rule_type="balance_sheet_identity",
                rule_name="资产=负债+所有者权益",
                expected=Decimal("300"),
                actual=Decimal("300"),
                diff=Decimal("0"),
                is_pass=True,
                severity=Severity.INFO,
            )
        ],
        anomalies=[],
        confidence=1.0,
        report_period="2024-12-31",
    )


class FakeReader:
    """Scripted StatementReader for the report path."""

    def __init__(
        self,
        data: ReportStatements | None = None,
        check: CheckResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.data = data
        self.check = check
        self.error = error
        self.check_requests: list[int] = []

    def fetch_report_statements(self, task_id: str) -> ReportStatements:
        """Return the configured report data."""
        if self.error is not None:
            raise self.error
        assert self.data is not None
        return self.data

    def fetch_year_ago_statements(
        self, company_code: str, current_period: str
    ) -> ReportStatements | None:
        """Unused on the report path."""
        return None

    def fetch_check_result(self, report_id: int) -> CheckResult | None:
        """Record the request and return the configured check result."""
        self.check_requests.append(report_id)
        return self.check


class FakeGenerator:
    """Scripted ReportGenerator duck-typing the generate contract."""

    def __init__(self, report: ReportResult) -> None:
        self.report = report
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        statement: Any,
        check_result: Any,
        *,
        kb_snippets: list[str] | None = None,
        company_name: str = "",
        company_code: str = "",
    ) -> ReportResult:
        """Record inputs and return the scripted report."""
        self.calls.append(
            {
                "statement": statement,
                "check_result": check_result,
                "company_name": company_name,
                "company_code": company_code,
            }
        )
        return self.report


class FakeChartRenderer:
    """Scripted ChartRenderer returning three deterministic charts."""

    def __init__(self, fallback: bool = False) -> None:
        self.fallback = fallback
        self.calls: list[Any] = []

    def render_all(self, statement: Any) -> list[ChartResult]:
        """Record the statement and return three chart results."""
        self.calls.append(statement)
        return [
            ChartResult(
                chart_type=chart_type,
                title=chart_type.chinese_name,
                png_bytes=f"png-{chart_type.value}".encode("utf-8"),
                fallback=self.fallback,
            )
            for chart_type in (
                ChartType.ASSET_STRUCTURE_PIE,
                ChartType.REVENUE_TREND_LINE,
                ChartType.CASH_FLOW_BAR,
            )
        ]


class FakePdfConverter:
    """Scripted PdfConverter returning a deterministic PDF."""

    def __init__(self, fallback: bool = False) -> None:
        self.fallback = fallback
        self.calls: list[dict[str, Any]] = []

    async def convert(
        self,
        report: Any,
        charts: list[ChartResult] | None = None,
        *,
        company_name: str = "",
        company_code: str = "",
    ) -> PdfResult:
        """Record inputs and return the scripted PDF result."""
        self.calls.append(
            {
                "report": report,
                "charts": charts,
                "company_name": company_name,
                "company_code": company_code,
            }
        )
        return PdfResult(
            pdf_bytes=b"%PDF-fake",
            fallback=self.fallback,
            chart_count=len(charts or []),
        )


def make_report(*, fallback: bool = False) -> ReportResult:
    """Build a 5-section ReportResult."""
    sections = [
        ReportSection(section_type=st, title=st.chinese_name, content=f"{st.chinese_name}内容")
        for st in ReportSectionType
    ]
    return ReportResult(
        sections=sections,
        report_period="2024-12-31",
        fallback=fallback,
        error="LLM 降级" if fallback else None,
    )


def build_message(step: str = "report", task_id: str = "task-r1") -> TaskMessage:
    """Build a valid report TaskMessage."""
    return TaskMessage(
        taskId=task_id,
        step=step,
        payload={},
        idempotencyKey=f"{task_id}:{step}",
    )


@pytest.fixture(autouse=True)
def _reset_report_handler() -> None:
    """Keep handler tests isolated from singleton state."""
    reset_handler()
    yield
    reset_handler()


# ---------------------------------------------------------------------------
# build_financial_statement 纯函数
# ---------------------------------------------------------------------------


def test_build_financial_statement_prefers_consolidated_current() -> None:
    """合并 + 本期优先；母公司/上期同名科目不进报表对象。"""
    data = ReportStatements(
        report_id=9,
        company_code="600000",
        report_period="2024-12-31",
        currency="CNY",
        unit="元",
        rows=(
            make_row("balance_sheet", "资产总计", 300),
            make_row("balance_sheet", "资产总计", 999, scope="母公司"),
            make_row("balance_sheet", "资产总计", 888, period_type="上期"),
        ),
    )
    statement = build_financial_statement(data)
    bs = statement.statements[StatementType.BALANCE_SHEET]
    assert len(bs) == 1
    assert bs[0].item == "资产总计"
    assert bs[0].value == 300.0


def test_build_financial_statement_skips_unknown_type_and_nulls() -> None:
    """未知 statement_type 与 NULL 值行跳过。"""
    data = ReportStatements(
        report_id=9,
        company_code="600000",
        report_period="2024-12-31",
        currency="CNY",
        unit="元",
        rows=(
            make_row("equity_change", "股本", 10),
            make_row("balance_sheet", "资产总计", 300),
            StatementRow("income_statement", "营业收入", None, "合并", "本期"),
        ),
    )
    statement = build_financial_statement(data)
    assert StatementType.BALANCE_SHEET in statement.statements
    assert StatementType.INCOME_STATEMENT not in statement.statements


# ---------------------------------------------------------------------------
# handle 契约
# ---------------------------------------------------------------------------


def test_handle_returns_m308_contract_payload() -> None:
    """Happy path：payload 匹配 M3.08 契约（ReportArtifactWriter 消费）。"""
    reader = FakeReader(data=make_data(), check=make_check_result())
    generator = FakeGenerator(make_report())
    renderer = FakeChartRenderer()
    converter = FakePdfConverter()
    configure_handler(
        reader=reader,
        generator=generator,
        chart_renderer=renderer,
        pdf_converter=converter,
    )

    payload = asyncio.run(handle(build_message()))

    assert set(payload.keys()) == {
        "report_period",
        "markdown",
        "pdf_b64",
        "charts",
        "fallback",
    }
    assert payload["report_period"] == "2024-12-31"
    # markdown 含 5 段标题。
    assert payload["markdown"].count("# ") == 5
    assert "公司概况" in payload["markdown"]
    # pdf_b64 可解码回原始字节。
    assert base64.b64decode(payload["pdf_b64"]) == b"%PDF-fake"
    # 3 张图表，chart_type 为小写 value（L2 端 toUpperCase 容错）。
    assert [c["chart_type"] for c in payload["charts"]] == [
        "asset_structure_pie",
        "revenue_trend_line",
        "cash_flow_bar",
    ]
    assert base64.b64decode(payload["charts"][0]["png_b64"]) == b"png-asset_structure_pie"
    assert payload["fallback"] is False
    # reader 取数路径正确。
    assert reader.check_requests == [9]
    # generator 收到公司名/代码（报告标题用）。
    assert generator.calls[0]["company_name"] == "浦发银行"
    assert generator.calls[0]["company_code"] == "600000"


def test_handle_aggregates_fallback_flags() -> None:
    """报告或 PDF 任一降级 → payload.fallback=True。"""
    configure_handler(
        reader=FakeReader(data=make_data(), check=make_check_result()),
        generator=FakeGenerator(make_report(fallback=True)),
        chart_renderer=FakeChartRenderer(),
        pdf_converter=FakePdfConverter(),
    )
    payload = asyncio.run(handle(build_message()))
    assert payload["fallback"] is True

    reset_handler()
    configure_handler(
        reader=FakeReader(data=make_data(), check=make_check_result()),
        generator=FakeGenerator(make_report()),
        chart_renderer=FakeChartRenderer(),
        pdf_converter=FakePdfConverter(fallback=True),
    )
    payload = asyncio.run(handle(build_message()))
    assert payload["fallback"] is True


def test_handle_chart_fallback_alone_keeps_payload_clean() -> None:
    """仅图表降级不改变整体 fallback 标记（占位图自身可展示）。"""
    configure_handler(
        reader=FakeReader(data=make_data(), check=make_check_result()),
        generator=FakeGenerator(make_report()),
        chart_renderer=FakeChartRenderer(fallback=True),
        pdf_converter=FakePdfConverter(),
    )
    payload = asyncio.run(handle(build_message()))
    assert payload["fallback"] is False


def test_handle_raises_when_reader_fails() -> None:
    """读库失败抛 AiException（DLQ 路径）。"""
    configure_handler(
        reader=FakeReader(error=AiException("MySQL connect failed")),
        generator=FakeGenerator(make_report()),
        chart_renderer=FakeChartRenderer(),
        pdf_converter=FakePdfConverter(),
    )
    with pytest.raises(AiException, match="MySQL connect failed"):
        asyncio.run(handle(build_message()))


def test_handle_raises_when_no_statement_rows() -> None:
    """EXTRACT 未写入科目行时显式失败。"""
    empty = ReportStatements(
        report_id=9,
        company_code="600000",
        report_period="2024-12-31",
        currency="CNY",
        unit="元",
        rows=(),
    )
    configure_handler(
        reader=FakeReader(data=empty, check=make_check_result()),
        generator=FakeGenerator(make_report()),
        chart_renderer=FakeChartRenderer(),
        pdf_converter=FakePdfConverter(),
    )
    with pytest.raises(AiException, match="no financial_statement rows"):
        asyncio.run(handle(build_message()))


def test_handle_raises_when_check_result_missing() -> None:
    """CHECK 未写入勾稽结果时显式失败（不静默生成无勾稽报告）。"""
    configure_handler(
        reader=FakeReader(data=make_data(), check=None),
        generator=FakeGenerator(make_report()),
        chart_renderer=FakeChartRenderer(),
        pdf_converter=FakePdfConverter(),
    )
    with pytest.raises(AiException, match="no accounting_check/anomaly rows"):
        asyncio.run(handle(build_message()))


def test_handle_rejects_unknown_step() -> None:
    """未知 step 显式报错走 DLQ。"""
    configure_handler(
        reader=FakeReader(data=make_data(), check=make_check_result()),
        generator=FakeGenerator(make_report()),
        chart_renderer=FakeChartRenderer(),
        pdf_converter=FakePdfConverter(),
    )
    with pytest.raises(ValueError, match="Unknown generator step"):
        asyncio.run(handle(build_message(step="check")))


def test_json_mode_hub_forces_json_mode() -> None:
    """_JsonModeHub 注入 json_mode=True 且不覆盖显式传参。"""
    inner = SimpleNamespace(settings=SimpleNamespace(), calls=[])
    inner.generate = lambda prompt, **kw: inner.calls.append((prompt, kw))

    adapter = _JsonModeHub(inner)
    adapter.generate("p1")
    adapter.generate("p2", json_mode=False)

    assert inner.calls[0][1]["json_mode"] is True
    assert inner.calls[1][1]["json_mode"] is False
