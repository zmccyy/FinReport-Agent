"""M5.02 工具单测：6 工具的行为与业务性失败语义。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.mysql_client import ReportStatements, StatementRow
from app.modules.agent.tools.check_accounting import make_check_accounting
from app.modules.agent.tools.compute_qoq import make_compute_qoq
from app.modules.agent.tools.compute_yoy import make_compute_yoy
from app.modules.agent.tools.query_statement import make_query_statement
from app.modules.agent.tools.search_kb import make_search_kb
from app.modules.agent.tools.unit_convert import make_unit_convert
from app.schemas.reasoning import Anomaly, CheckResult, RuleResult, RuleType

REPORT_ID = 42


def _row(item: str, value: float, *, scope: str = "合并", period: str = "本期") -> StatementRow:
    return StatementRow(
        statement_type="balance_sheet",
        item_name=item,
        item_value=Decimal(str(value)),
        scope=scope,
        period_type=period,
    )


class FakeReader:
    """StatementReader 的内存实现（按 report_id/company 提供数据）。"""

    def __init__(
        self,
        *,
        current: ReportStatements | None = None,
        prior: ReportStatements | None = None,
        check: CheckResult | None = None,
        missing_report: bool = False,
    ) -> None:
        self.current = current
        self.prior = prior
        self.check = check
        self.missing_report = missing_report

    def fetch_report_statements(self, task_id: str) -> ReportStatements:
        raise NotImplementedError("工具场景不使用 task_id 查询")

    def fetch_report_statements_by_report_id(self, report_id: int) -> ReportStatements | None:
        return None if self.missing_report else self.current

    def fetch_year_ago_statements(
        self, company_code: str, current_period: str
    ) -> ReportStatements | None:
        return self.prior

    def fetch_check_result(self, report_id: int) -> CheckResult | None:
        return self.check


def _make_statements(rows: list[StatementRow]) -> ReportStatements:
    return ReportStatements(
        report_id=REPORT_ID,
        company_code="600519",
        company_name="贵州茅台",
        report_period="2025-12-31",
        currency="CNY",
        unit="元",
        rows=tuple(rows),
    )


# ---------------------------------------------------------------------------
# query_statement
# ---------------------------------------------------------------------------


def test_query_statement_returns_value() -> None:
    reader = FakeReader(current=_make_statements([_row("货币资金", 5.169e10)]))
    tool = make_query_statement(reader, REPORT_ID)
    result = tool.handler({"item": "货币资金"})
    assert result["ok"] is True
    data = result["data"]
    assert data["value"] == pytest.approx(5.169e10)
    assert data["period"] == "本期"
    assert data["unit"] == "元"
    assert data["company_name"] == "贵州茅台"


def test_query_statement_filters_by_report_type_and_period() -> None:
    reader = FakeReader(
        current=_make_statements(
            [
                _row("货币资金", 1.0, scope="合并", period="本期"),
                _row("货币资金", 2.0, scope="合并", period="上期"),
                _row("货币资金", 3.0, scope="母公司", period="本期"),
            ]
        )
    )
    tool = make_query_statement(reader, REPORT_ID)
    assert tool.handler({"item": "货币资金"})["data"]["value"] == 1.0
    assert tool.handler({"item": "货币资金", "period": "上期"})["data"]["value"] == 2.0
    assert tool.handler({"item": "货币资金", "period": "本期", "report_type": "balance_sheet"})["data"]["value"] == 1.0


def test_query_statement_business_missing_item() -> None:
    reader = FakeReader(current=_make_statements([_row("货币资金", 1.0)]))
    tool = make_query_statement(reader, REPORT_ID)
    result = tool.handler({"item": "不存在科目"})
    assert result["ok"] is True  # 业务性缺失不消耗报错计数
    assert result["data"] is None
    assert "未找到" in result["reason"]


def test_query_statement_unknown_report_type() -> None:
    reader = FakeReader(current=_make_statements([_row("货币资金", 1.0)]))
    tool = make_query_statement(reader, REPORT_ID)
    result = tool.handler({"item": "货币资金", "report_type": "损益表"})
    assert result["ok"] is True
    assert result["data"] is None


def test_query_statement_missing_report() -> None:
    tool = make_query_statement(FakeReader(missing_report=True), REPORT_ID)
    result = tool.handler({"item": "货币资金"})
    assert result["ok"] is True
    assert result["data"] is None
    assert "报表不存在" in result["reason"]


# ---------------------------------------------------------------------------
# compute_yoy
# ---------------------------------------------------------------------------


def test_compute_yoy_returns_percent() -> None:
    reader = FakeReader(
        current=_make_statements([_row("营业收入", 1.688e12)]),
        prior=_make_statements([_row("营业收入", 1.709e12)]),
    )
    tool = make_compute_yoy(reader, REPORT_ID)
    result = tool.handler({"item": "营业收入"})
    assert result["ok"] is True
    yoy = result["data"]["yoy_pct"]
    assert yoy == pytest.approx(-1.23, abs=0.1)  # (1.688-1.709)/1.709


def test_compute_yoy_missing_prior_data() -> None:
    reader = FakeReader(
        current=_make_statements([_row("营业收入", 1.688e12)]),
        prior=None,
    )
    tool = make_compute_yoy(reader, REPORT_ID)
    result = tool.handler({"item": "营业收入"})
    assert result["ok"] is True
    assert result["data"] is None
    assert "同比数据不足" in result["reason"]


# ---------------------------------------------------------------------------
# compute_qoq（年报场景明确不可计算）
# ---------------------------------------------------------------------------


def test_compute_qoq_annual_report_not_calculable() -> None:
    reader = FakeReader(
        current=_make_statements(
            [_row("营业收入", 1.688e12, period="本期"), _row("营业收入", 1.709e12, period="上期")]
        )
    )
    tool = make_compute_qoq(reader, REPORT_ID)
    result = tool.handler({"item": "营业收入"})
    assert result["ok"] is True
    assert result["data"] is None
    assert "环比" in result["reason"]
    assert "compute_yoy" in result["reason"]


# ---------------------------------------------------------------------------
# check_accounting
# ---------------------------------------------------------------------------


def test_check_accounting_returns_rules_and_anomalies() -> None:
    check = CheckResult(
        rules=[
            RuleResult(
                rule_type=RuleType.BALANCE_SHEET_IDENTITY,
                rule_name="资产=负债+所有者权益",
                diff=Decimal("0.0"),
                is_pass=True,
            )
        ],
        anomalies=[
            Anomaly(item_name="存货", anomaly_type="yoy_change", description="存货同比大增")
        ],
        confidence=0.92,
    )
    tool = make_check_accounting(FakeReader(check=check), REPORT_ID)
    result = tool.handler({})
    assert result["ok"] is True
    assert result["data"]["rules"][0]["rule_name"] == "资产=负债+所有者权益"
    assert result["data"]["rules"][0]["is_pass"] is True
    assert result["data"]["anomalies"][0]["item_name"] == "存货"
    assert result["data"]["confidence"] == 0.92


def test_check_accounting_missing_result() -> None:
    tool = make_check_accounting(FakeReader(check=None), REPORT_ID)
    result = tool.handler({})
    assert result["ok"] is True
    assert result["data"] is None
    assert "未生成" in result["reason"]


# ---------------------------------------------------------------------------
# search_kb（知识库未就绪 → 业务性提示，不抛异常）
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """embed 契约桩（返回固定 512 维向量）。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [[0.0] * 512 for _ in texts]


def test_search_kb_reports_not_ready_when_milvus_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Milvus 未就绪（M5.07 前）→ ok=True + data=null + reason，不抛异常。"""

    def boom_connect(*args, **kwargs):
        raise ConnectionError("milvus not running")

    monkeypatch.setattr("pymilvus.connections.connect", boom_connect)
    tool = make_search_kb(FakeEmbedder(), milvus_host="localhost", milvus_port=19530)
    result = tool.handler({"keywords": "产能扩张"})
    assert result["ok"] is True
    assert result["data"] is None
    assert "知识库" in result["reason"]


def test_search_kb_returns_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Milvus 就绪 → 返回 hits（mock collection）。"""

    class FakeCollection:
        def load(self) -> None:
            pass

        def search(self, *args, **kwargs):
            assert kwargs["anns_field"] == "embedding"
            assert kwargs["limit"] == 3
            hit = type("Hit", (), {})()
            hit.score = 0.9123
            hit.entity = {
                "text": "茅台 2025 年实现营业收入…",
                "page": 6,
                "doc_id": 1,
                "chunk_type": "TEXT",
            }
            return [[hit]]

    def fake_collection(name):
        assert name == "fin_kb"
        return FakeCollection()

    def fake_connect(*args, **kwargs):
        return None

    monkeypatch.setattr("pymilvus.connections.connect", fake_connect)
    monkeypatch.setattr("pymilvus.Collection", fake_collection)
    tool = make_search_kb(FakeEmbedder(), milvus_host="localhost", milvus_port=19530)
    result = tool.handler({"keywords": "营业收入"})
    assert result["ok"] is True
    hits = result["data"]["hits"]
    assert hits[0]["score"] == pytest.approx(0.9123)
    assert hits[0]["page"] == 6


# ---------------------------------------------------------------------------
# unit_convert
# ---------------------------------------------------------------------------


def test_unit_convert_basic() -> None:
    tool = make_unit_convert()
    result = tool.handler({"value": 1, "source_unit": "亿元", "target_unit": "万元"})
    assert result["ok"] is True
    assert result["data"]["value"] == pytest.approx(10000.0)


def test_unit_convert_back_to_yuan() -> None:
    tool = make_unit_convert()
    result = tool.handler({"value": 1.5, "source_unit": "万元", "target_unit": "元"})
    assert result["data"]["value"] == pytest.approx(15000.0)


def test_unit_convert_unknown_unit() -> None:
    tool = make_unit_convert()
    result = tool.handler({"value": 1, "source_unit": "公斤", "target_unit": "元"})
    assert result["ok"] is True
    assert result["data"] is None
    assert "未知源单位" in result["reason"]


def test_unit_convert_non_numeric_value() -> None:
    tool = make_unit_convert()
    result = tool.handler({"value": "abc", "source_unit": "元", "target_unit": "万元"})
    assert result["ok"] is True
    assert result["data"] is None
