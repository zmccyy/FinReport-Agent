"""M3.01 Reasoner 勾稽规则引擎测试（spec §2.3 M8 + plan M3.01）。

测试覆盖：

1. **茅台样本** — 规则 1（资产负债恒等式）应通过
2. **不平衡用例** — 规则 1 应失败、severity=ERROR、diff 字段正确
3. **科目缺失** — 必需科目缺失返回 CRITICAL + missing_items
4. **同义词匹配** — "资产合计" vs "资产总计" 都能命中
5. **规则 2 边界** — 缺盈余公积/应付股利时容差放宽到净利润 5%
6. **规则 3 边界** — 缺折旧摊销时容差放宽到净利润 20%
7. **RuleEngine 聚合** — confidence 计算正确、CRITICAL 额外扣分
8. **规则异常降级** — 规则抛异常时引擎不阻断、转 CRITICAL
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.reasoner.accounting_rules import (
    BalanceSheetIdentityRule,
    CashFlowVsNetIncomeRule,
    NetIncomeToRetainedEarningsRule,
)
from app.modules.reasoner.rule_engine import RuleEngine
from app.schemas.reasoning import (
    CheckResult,
    RuleResult,
    RuleType,
    Severity,
    StatementSnapshot,
)
from app.schemas.statement import StatementType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _snapshot(
    bs: dict[str, Decimal] | None = None,
    is_: dict[str, Decimal] | None = None,
    cf: dict[str, Decimal] | None = None,
    report_period: str = "2025-12-31",
) -> StatementSnapshot:
    """构造 StatementSnapshot 测试辅助。"""
    statements: dict[StatementType, dict[str, Decimal]] = {}
    if bs is not None:
        statements[StatementType.BALANCE_SHEET] = bs
    if is_ is not None:
        statements[StatementType.INCOME_STATEMENT] = is_
    if cf is not None:
        statements[StatementType.CASH_FLOW] = cf
    return StatementSnapshot(
        report_period=report_period,
        currency="CNY",
        unit="元",
        statements=statements,
    )


@pytest.fixture
def moutai_snapshot() -> StatementSnapshot:
    """茅台 2025 年报样本（来自 data/benchmark/ground_truth/moutai_2025_sample.json）。

    数值为近似值：

    * 资产总计 280亿 = 负债合计 60亿 + 所有者权益合计 220亿（恒等式成立）
    * 净利润 850亿（无未分配利润数据，规则 2 CRITICAL）
    * 经营现金流净额 900亿（与净利润 850亿 差 50亿 < 5%，规则 3 通过）
    """
    return _snapshot(
        bs={
            "货币资金": Decimal("59000000000.0"),
            "资产总计": Decimal("280000000000.0"),
            "负债合计": Decimal("60000000000.0"),
            "所有者权益合计": Decimal("220000000000.0"),
        },
        is_={
            "营业收入": Decimal("170000000000.0"),
            "净利润": Decimal("85000000000.0"),
        },
        cf={
            "经营活动产生的现金流量净额": Decimal("90000000000.0"),
        },
    )


# ---------------------------------------------------------------------------
# 规则 1：资产负债恒等式
# ---------------------------------------------------------------------------


class TestBalanceSheetIdentityRule:
    """规则 1 — 资产 = 负债 + 所有者权益。"""

    def test_should_pass_for_moutai_sample(
        self, moutai_snapshot: StatementSnapshot
    ) -> None:
        """茅台样本：280亿 = 60亿 + 220亿，规则 1 应通过。"""
        rule = BalanceSheetIdentityRule()
        result = rule.check(moutai_snapshot)

        assert result.rule_type == RuleType.BALANCE_SHEET_IDENTITY
        assert result.is_pass is True
        assert result.severity == Severity.INFO
        assert result.expected == Decimal("280000000000.0")
        assert result.actual == Decimal("280000000000.0")
        assert abs(result.diff or Decimal("0")) <= Decimal("0.01")
        assert result.missing_items == []

    def test_should_fail_when_balance_sheet_not_balanced(self) -> None:
        """资产 != 负债 + 权益，规则 1 应失败、severity=ERROR。"""
        snapshot = _snapshot(
            bs={
                "资产总计": Decimal("280000000000.0"),
                "负债合计": Decimal("60000000000.0"),
                "所有者权益合计": Decimal("210000000000.0"),  # 故意少 10亿
            },
        )
        rule = BalanceSheetIdentityRule()
        result = rule.check(snapshot)

        assert result.is_pass is False
        assert result.severity == Severity.ERROR
        assert result.expected == Decimal("270000000000.0")
        assert result.actual == Decimal("280000000000.0")
        assert result.diff == Decimal("10000000000.0")
        assert "资产负债恒等式不成立" in result.note

    def test_should_return_critical_when_required_items_missing(self) -> None:
        """缺资产总计，规则 1 应 CRITICAL + missing_items 填充代表名。"""
        snapshot = _snapshot(
            bs={
                "负债合计": Decimal("60000000000.0"),
                "所有者权益合计": Decimal("220000000000.0"),
            },
        )
        rule = BalanceSheetIdentityRule()
        result = rule.check(snapshot)

        assert result.is_pass is False
        assert result.severity == Severity.CRITICAL
        # 新 require 语义：missing_items 只含同义词组第一个代表名。
        assert "资产总计" in result.missing_items
        assert "资产合计" not in result.missing_items  # 同义词不再全部进入 missing
        assert "缺失必需科目" in result.note
        assert result.expected is None
        assert result.actual is None

    def test_should_match_synonym_asset_total(self) -> None:
        """科目名变体"资产合计"应被识别为资产总计。"""
        snapshot = _snapshot(
            bs={
                "资产合计": Decimal("280000000000.0"),
                "负债合计": Decimal("60000000000.0"),
                "所有者权益合计": Decimal("220000000000.0"),
            },
        )
        rule = BalanceSheetIdentityRule()
        result = rule.check(snapshot)

        assert result.is_pass is True


# ---------------------------------------------------------------------------
# 规则 2：净利润 → 未分配利润变动
# ---------------------------------------------------------------------------


class TestNetIncomeToRetainedEarningsRule:
    """规则 2 — 未分配利润 ≈ 净利润 - 调整项。"""

    def test_should_pass_when_retained_equals_net_income(self) -> None:
        """未分配利润 = 净利润（无调整项），规则 2 应通过。"""
        snapshot = _snapshot(
            bs={"未分配利润": Decimal("85000000000.0")},
            is_={"净利润": Decimal("85000000000.0")},
        )
        rule = NetIncomeToRetainedEarningsRule()
        result = rule.check(snapshot)

        assert result.is_pass is True
        assert result.severity == Severity.INFO

    def test_should_pass_with_relative_tolerance_when_no_adjustments(self) -> None:
        """缺调整项时容差放宽到净利润 5%；差异 4% 应通过。"""
        snapshot = _snapshot(
            bs={"未分配利润": Decimal("88400000000.0")},  # 比净利润多 4%
            is_={"净利润": Decimal("85000000000.0")},
        )
        rule = NetIncomeToRetainedEarningsRule()
        result = rule.check(snapshot)

        assert result.is_pass is True
        assert result.tolerance >= Decimal("4250000000.0")  # 850亿 × 5%

    def test_should_fail_when_diff_exceeds_relative_tolerance(self) -> None:
        """缺调整项时差异 10% 应失败、severity=WARN。"""
        snapshot = _snapshot(
            bs={"未分配利润": Decimal("93500000000.0")},  # 比净利润多 10%
            is_={"净利润": Decimal("85000000000.0")},
        )
        rule = NetIncomeToRetainedEarningsRule()
        result = rule.check(snapshot)

        assert result.is_pass is False
        assert result.severity == Severity.WARN
        assert "缺盈余公积/应付股利" in result.note

    def test_should_return_critical_when_net_income_missing(self) -> None:
        """缺净利润，规则 2 应 CRITICAL。"""
        snapshot = _snapshot(
            bs={"未分配利润": Decimal("85000000000.0")},
            is_={"营业收入": Decimal("170000000000.0")},  # 没有"净利润"
        )
        rule = NetIncomeToRetainedEarningsRule()
        result = rule.check(snapshot)

        assert result.is_pass is False
        assert result.severity == Severity.CRITICAL
        assert any("净利润" in name for name in result.missing_items)

    def test_should_use_strict_tolerance_when_adjustments_present(self) -> None:
        """有调整项时用绝对容差 0.01；差异 1 元应通过。"""
        snapshot = _snapshot(
            bs={
                "未分配利润": Decimal("84000000000.0"),
                "提取盈余公积": Decimal("1000000000.0"),  # 10亿盈余公积
            },
            is_={"净利润": Decimal("85000000000.0")},
        )
        rule = NetIncomeToRetainedEarningsRule()
        result = rule.check(snapshot)

        # expected = 850亿 - 10亿 = 840亿，actual = 840亿，diff = 0
        assert result.is_pass is True
        assert result.tolerance == Decimal("0.01")


# ---------------------------------------------------------------------------
# 规则 3：经营现金流 vs 净利润
# ---------------------------------------------------------------------------


class TestCashFlowVsNetIncomeRule:
    """规则 3 — 经营现金流 ≈ 净利润 + 折旧摊销。"""

    def test_should_pass_for_moutai_sample(
        self, moutai_snapshot: StatementSnapshot
    ) -> None:
        """茅台样本：CF 900亿 vs NI 850亿，差 50亿 < 5%（42.5亿）...

        实际差异 50亿 > 5% 阈值 42.5亿，应 WARN 失败。
        但 moutai 样本无折旧摊销，容差放宽到 20%（170亿），50亿 < 170亿 通过。
        """
        rule = CashFlowVsNetIncomeRule()
        result = rule.check(moutai_snapshot)

        assert result.is_pass is True
        assert result.severity == Severity.INFO
        assert result.tolerance >= Decimal("17000000000.0")  # 850亿 × 20%

    def test_should_pass_when_depreciation_present(self) -> None:
        """有折旧摊销时用绝对容差。"""
        snapshot = _snapshot(
            is_={"净利润": Decimal("85000000000.0")},
            cf={
                "经营活动产生的现金流量净额": Decimal("95000000000.0"),
                "固定资产折旧": Decimal("10000000000.0"),  # 100亿折旧
            },
        )
        rule = CashFlowVsNetIncomeRule()
        result = rule.check(snapshot)

        # expected = 850亿 + 100亿 = 950亿，actual = 950亿，diff = 0
        assert result.is_pass is True
        assert result.tolerance == Decimal("0.01")

    def test_should_fail_when_diff_exceeds_relative_tolerance(self) -> None:
        """无折旧摊销时差异 30% 应失败。"""
        snapshot = _snapshot(
            is_={"净利润": Decimal("85000000000.0")},
            cf={
                "经营活动产生的现金流量净额": Decimal("200000000000.0")
            },  # 200亿，差 135%
        )
        rule = CashFlowVsNetIncomeRule()
        result = rule.check(snapshot)

        assert result.is_pass is False
        assert result.severity == Severity.WARN
        assert "缺折旧摊销" in result.note

    def test_should_return_critical_when_ocf_missing(self) -> None:
        """缺经营现金流净额，规则 3 应 CRITICAL。"""
        snapshot = _snapshot(
            is_={"净利润": Decimal("85000000000.0")},
            cf={"投资活动产生的现金流量净额": Decimal("10000000000.0")},  # 不是经营活动
        )
        rule = CashFlowVsNetIncomeRule()
        result = rule.check(snapshot)

        assert result.is_pass is False
        assert result.severity == Severity.CRITICAL
        assert any("经营活动" in name for name in result.missing_items)


# ---------------------------------------------------------------------------
# RuleEngine 聚合
# ---------------------------------------------------------------------------


class TestRuleEngine:
    """RuleEngine 聚合测试。"""

    def test_should_run_all_three_rules(
        self, moutai_snapshot: StatementSnapshot
    ) -> None:
        """茅台样本：3 条规则全部执行；规则 1/3 通过，规则 2 CRITICAL。"""
        engine = RuleEngine()
        result = engine.check(moutai_snapshot)

        assert isinstance(result, CheckResult)
        assert len(result.rules) == 3
        assert result.rules[0].rule_type == RuleType.BALANCE_SHEET_IDENTITY
        assert result.rules[1].rule_type == RuleType.NET_INCOME_TO_RETAINED
        assert result.rules[2].rule_type == RuleType.CASH_FLOW_VS_NET_INCOME
        assert result.rules[0].is_pass is True  # 资产负债恒等
        assert result.rules[1].is_pass is False  # 缺未分配利润
        assert result.rules[1].severity == Severity.CRITICAL
        assert result.rules[2].is_pass is True  # CF vs NI
        assert result.report_period == "2025-12-31"

    def test_should_compute_confidence_with_critical_penalty(
        self, moutai_snapshot: StatementSnapshot
    ) -> None:
        """茅台样本：2/3 通过 + 1 CRITICAL → confidence = 2/3 - 0.2 ≈ 0.467。"""
        engine = RuleEngine()
        result = engine.check(moutai_snapshot)

        # 2/3 - 0.2 = 0.4666...
        assert 0.46 <= result.confidence <= 0.48

    def test_should_return_full_confidence_when_all_pass(self) -> None:
        """三表齐全且恒等，confidence = 1.0。"""
        snapshot = _snapshot(
            bs={
                "资产总计": Decimal("280000000000.0"),
                "负债合计": Decimal("60000000000.0"),
                "所有者权益合计": Decimal("220000000000.0"),
                "未分配利润": Decimal("85000000000.0"),
            },
            is_={"净利润": Decimal("85000000000.0")},
            cf={"经营活动产生的现金流量净额": Decimal("85000000000.0")},
        )
        engine = RuleEngine()
        result = engine.check(snapshot)

        assert result.all_pass is True
        assert result.confidence == 1.0

    def test_register_should_append_custom_rule(self) -> None:
        """register 应追加规则到列表末尾。"""
        engine = RuleEngine()
        initial_count = len(engine.rules)

        class DummyRule:
            rule_type = RuleType.BALANCE_SHEET_IDENTITY

            def check(self, snapshot: StatementSnapshot) -> RuleResult:
                return RuleResult(
                    rule_type=RuleType.BALANCE_SHEET_IDENTITY,
                    rule_name="dummy",
                    is_pass=True,
                )

        engine.register(DummyRule())  # type: ignore[arg-type]
        assert len(engine.rules) == initial_count + 1


# ---------------------------------------------------------------------------
# 异常降级（单独 class 以便用 monkeypatch）
# ---------------------------------------------------------------------------


class TestRuleEngineExceptionHandling:
    """规则异常降级测试。"""

    def test_should_convert_rule_exception_to_critical_result(self) -> None:
        """规则抛 Exception 时引擎应转 CRITICAL、不阻断其他规则。"""
        engine = RuleEngine()
        # 替换第一条规则为会抛异常的 stub
        original_rules = engine._rules.copy()

        class _BoomRule:
            rule_type = RuleType.BALANCE_SHEET_IDENTITY

            def check(self, snapshot: StatementSnapshot) -> RuleResult:
                raise RuntimeError("boom")

        engine._rules = [_BoomRule()] + original_rules[1:]  # type: ignore[list-item]

        snapshot = _snapshot(
            bs={
                "资产总计": Decimal("280000000000.0"),
                "负债合计": Decimal("60000000000.0"),
                "所有者权益合计": Decimal("220000000000.0"),
                "未分配利润": Decimal("85000000000.0"),
            },
            is_={"净利润": Decimal("85000000000.0")},
            cf={"经营活动产生的现金流量净额": Decimal("85000000000.0")},
        )
        result = engine.check(snapshot)

        assert len(result.rules) == 3
        assert result.rules[0].is_pass is False
        assert result.rules[0].severity == Severity.CRITICAL
        assert "boom" in result.rules[0].note
        # 后续规则不受影响
        assert result.rules[1].rule_type == RuleType.NET_INCOME_TO_RETAINED
        assert result.rules[2].rule_type == RuleType.CASH_FLOW_VS_NET_INCOME


# ---------------------------------------------------------------------------
# StatementSnapshot.from_financial_statement 集成
# ---------------------------------------------------------------------------


class TestStatementSnapshotConversion:
    """StatementSnapshot 从 FinancialStatement 构造。"""

    def test_should_convert_financial_statement_to_snapshot(self) -> None:
        """from_financial_statement 应正确转换三表数据。"""
        from app.schemas.statement import (
            FinancialStatement,
            StatementItem,
        )

        fs = FinancialStatement(
            report_period="2025-12-31",
            currency="CNY",
            unit="元",
            statements={
                StatementType.BALANCE_SHEET: [
                    StatementItem(item="资产总计", value=280000000000.0),
                    StatementItem(item="负债合计", value=60000000000.0),
                    StatementItem(item="所有者权益合计", value=220000000000.0),
                ],
                StatementType.INCOME_STATEMENT: [
                    StatementItem(item="净利润", value=85000000000.0),
                ],
            },
        )
        snapshot = StatementSnapshot.from_financial_statement(fs)

        assert snapshot.report_period == "2025-12-31"
        assert snapshot.unit == "元"
        assert snapshot.get(StatementType.BALANCE_SHEET, "资产总计") == Decimal(
            "280000000000.0"
        )
        assert snapshot.get(StatementType.INCOME_STATEMENT, "净利润") == Decimal(
            "85000000000.0"
        )
        assert snapshot.get(StatementType.CASH_FLOW, "不存在") is None

    def test_to_dict_should_be_json_serializable(
        self, moutai_snapshot: StatementSnapshot
    ) -> None:
        """CheckResult.to_dict 应可 JSON 序列化（L3 → L2 progress payload）。"""
        import json

        engine = RuleEngine()
        result = engine.check(moutai_snapshot)
        payload = result.to_dict()

        serialized = json.dumps(payload, ensure_ascii=False)
        assert "rules" in serialized
        assert "anomalies" in serialized
        assert "confidence" in serialized
