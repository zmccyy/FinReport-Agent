"""M4.05 check handler 单测：只读 MySQL 拉三表 + 勾稽/异常/LLM 复核."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.core.exceptions import AiException
from app.core.mysql_client import ReportStatements, StatementRow
from app.modules.reasoner.handler import (
    build_snapshot,
    configure_handler,
    handle,
    reset_handler,
)
from app.schemas.reasoning import CheckResult, StatementSnapshot
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


def make_data(
    *,
    task_id: str = "task-c1",
    period: str = "2024-12-31",
    extra_rows: tuple[StatementRow, ...] = (),
) -> ReportStatements:
    """Build a consistent BS dataset (资产 = 负债 + 权益)."""
    rows = (
        make_row("balance_sheet", "资产总计", 300),
        make_row("balance_sheet", "负债合计", 200),
        make_row("balance_sheet", "所有者权益合计", 100),
        make_row("income_statement", "营业收入", 80),
        make_row("income_statement", "净利润", 12),
        make_row("cash_flow", "经营活动产生的现金流量净额", 20),
        *extra_rows,
    )
    return ReportStatements(
        report_id=7,
        company_code="600000",
        report_period=period,
        currency="CNY",
        unit="元",
        rows=rows,
    )


class FakeReader:
    """Scripted StatementReader serving current + optional year-ago data."""

    def __init__(
        self,
        data: ReportStatements | None = None,
        year_ago: ReportStatements | None = None,
        error: Exception | None = None,
    ) -> None:
        self.data = data
        self.year_ago = year_ago
        self.error = error
        self.year_ago_requests: list[tuple[str, str]] = []

    def fetch_report_statements(self, task_id: str) -> ReportStatements:
        """Return the configured current report data."""
        if self.error is not None:
            raise self.error
        assert self.data is not None
        return self.data

    def fetch_year_ago_statements(
        self, company_code: str, current_period: str
    ) -> ReportStatements | None:
        """Record the request and return the configured comparison data."""
        self.year_ago_requests.append((company_code, current_period))
        return self.year_ago


class FakeReviewer:
    """Scripted LLMReviewer：记录调用、按脚本回填 note。"""

    def __init__(self, note: str = "科目分类差异，可解释") -> None:
        self.note = note
        self.calls: list[tuple[CheckResult, StatementSnapshot]] = []

    async def review(self, check_result: CheckResult, snapshot: StatementSnapshot) -> CheckResult:
        """Fill note on every WARN/ERROR rule, mirroring the real reviewer."""
        self.calls.append((check_result, snapshot))
        new_rules = []
        for rule in check_result.rules:
            if rule.severity.value in ("WARN", "ERROR"):
                rule = rule.model_copy(update={"note": self.note, "llm_reviewed": True})
            new_rules.append(rule)
        return check_result.model_copy(update={"rules": new_rules})


def build_message(step: str = "check", task_id: str = "task-c1") -> TaskMessage:
    """Build a valid reasoner TaskMessage."""
    return TaskMessage(
        taskId=task_id,
        step=step,
        payload={},
        idempotencyKey=f"{task_id}:{step}",
    )


@pytest.fixture(autouse=True)
def _reset_check_handler() -> None:
    """Keep handler tests isolated from singleton state."""
    reset_handler()
    yield
    reset_handler()


# ---------------------------------------------------------------------------
# build_snapshot 纯函数
# ---------------------------------------------------------------------------


def test_build_snapshot_prefers_consolidated_current_period() -> None:
    """合并 + 本期优先；母公司/上期同名科目不参与快照。"""
    data = make_data(
        extra_rows=(
            make_row("balance_sheet", "资产总计", 999, scope="母公司", period_type="本期"),
            make_row("balance_sheet", "资产总计", 888, scope="合并", period_type="上期"),
        )
    )
    snapshot = build_snapshot(data)
    assert snapshot.statements[StatementType.BALANCE_SHEET]["资产总计"] == Decimal("300")


def test_build_snapshot_falls_back_when_no_preferred_rows() -> None:
    """只有母公司行时保留母公司数据（降级而非丢弃）。"""
    data = ReportStatements(
        report_id=7,
        company_code="600000",
        report_period="2024-12-31",
        currency="CNY",
        unit="元",
        rows=(make_row("balance_sheet", "资产总计", 120, scope="母公司"),),
    )
    snapshot = build_snapshot(data)
    assert snapshot.statements[StatementType.BALANCE_SHEET]["资产总计"] == Decimal("120")


def test_build_snapshot_skips_unknown_statement_type() -> None:
    """未知 statement_type 行跳过，不炸整个快照。"""
    data = make_data(extra_rows=(make_row("equity_change", "股本", 10),))
    snapshot = build_snapshot(data)
    assert StatementType.BALANCE_SHEET in snapshot.statements
    assert len(snapshot.statements) == 3


# ---------------------------------------------------------------------------
# handle 契约
# ---------------------------------------------------------------------------


def test_handle_check_returns_m304_contract_payload() -> None:
    """Happy path：payload 匹配 M3.04 契约（CheckResultWriter 消费）。"""
    reader = FakeReader(data=make_data())
    reviewer = FakeReviewer()
    configure_handler(reader=reader, reviewer=reviewer)

    payload = asyncio.run(handle(build_message()))

    assert set(payload.keys()) == {"rules", "anomalies", "confidence", "report_period"}
    assert payload["report_period"] == "2024-12-31"
    assert len(payload["rules"]) == 3
    identity = next(r for r in payload["rules"] if r["rule_type"] == "balance_sheet_identity")
    assert identity["is_pass"] is True
    # Decimal → str（model_dump mode="json"，L2 CheckResultWriter 解析字符串）。
    assert identity["actual"] == "300"
    assert reader.year_ago_requests == [("600000", "2024-12-31")]


def test_handle_check_runs_yoy_anomaly_detection() -> None:
    """同比对比期存在且变动超阈值时产出 anomalies。"""
    year_ago = ReportStatements(
        report_id=3,
        company_code="600000",
        report_period="2023-12-31",
        currency="CNY",
        unit="元",
        rows=(
            make_row("balance_sheet", "资产总计", 100),
            make_row("balance_sheet", "负债合计", 60),
            make_row("balance_sheet", "所有者权益合计", 40),
            make_row("income_statement", "营业收入", 10),
            make_row("income_statement", "净利润", 2),
            make_row("cash_flow", "经营活动产生的现金流量净额", 5),
        ),
    )
    reader = FakeReader(data=make_data(), year_ago=year_ago)
    configure_handler(reader=reader, reviewer=FakeReviewer())

    payload = asyncio.run(handle(build_message()))

    # 资产总计 100 → 300（+200%），必触发同比异常。
    assert payload["anomalies"]
    asset = [a for a in payload["anomalies"] if a["item_name"] == "资产总计"]
    assert asset
    assert asset[0]["anomaly_type"] == "yoy_change"


def test_handle_check_degrades_without_year_ago() -> None:
    """无同比历史时 anomalies 为空但勾稽照常返回。"""
    reader = FakeReader(data=make_data(), year_ago=None)
    configure_handler(reader=reader, reviewer=FakeReviewer())

    payload = asyncio.run(handle(build_message()))

    assert payload["anomalies"] == []
    assert len(payload["rules"]) == 3


def test_handle_check_invokes_reviewer_on_failed_rules() -> None:
    """LLM 复核只针对 WARN/ERROR 规则（fake 镜像真实行为）。"""
    # 破坏恒等式：资产 300 ≠ 200 + 150。
    data = ReportStatements(
        report_id=7,
        company_code="600000",
        report_period="2024-12-31",
        currency="CNY",
        unit="元",
        rows=(
            make_row("balance_sheet", "资产总计", 300),
            make_row("balance_sheet", "负债合计", 200),
            make_row("balance_sheet", "所有者权益合计", 150),
        ),
    )
    reviewer = FakeReviewer()
    configure_handler(reader=FakeReader(data=data), reviewer=reviewer)

    payload = asyncio.run(handle(build_message()))

    assert reviewer.calls, "reviewer must be invoked"
    identity = next(r for r in payload["rules"] if r["rule_type"] == "balance_sheet_identity")
    assert identity["is_pass"] is False
    assert identity["llm_reviewed"] is True
    assert identity["note"]


def test_handle_check_raises_when_reader_fails() -> None:
    """读库失败抛 AiException（DLQ 路径）。"""
    configure_handler(
        reader=FakeReader(error=AiException("MySQL connect failed")),
        reviewer=FakeReviewer(),
    )
    with pytest.raises(AiException, match="MySQL connect failed"):
        asyncio.run(handle(build_message()))


def test_handle_check_raises_when_no_statement_rows() -> None:
    """EXTRACT 未写入数据时显式失败，而非产出全 CRITICAL 空洞结果。"""
    empty = ReportStatements(
        report_id=7,
        company_code="600000",
        report_period="2024-12-31",
        currency="CNY",
        unit="元",
        rows=(),
    )
    configure_handler(reader=FakeReader(data=empty), reviewer=FakeReviewer())
    with pytest.raises(AiException, match="no financial_statement rows"):
        asyncio.run(handle(build_message()))


def test_handle_report_step_stays_mock_until_m406() -> None:
    """report 步骤暂留 mock（M4.06 generator 归位）。"""
    payload = asyncio.run(handle(build_message(step="report")))
    assert payload == {"operation": "report", "status": "mock-complete"}


def test_handle_rejects_unknown_step() -> None:
    """未知 step 显式报错走 DLQ。"""
    with pytest.raises(ValueError, match="Unknown reasoner step"):
        asyncio.run(handle(build_message(step="reason")))
