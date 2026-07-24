"""M3.03 AnomalyDetector 测试（spec §2.3 M8 + plan M3.03）。

测试用例分布：

* ``TestAnomalyType`` — 枚举值与中文名。
* ``TestChangeRatio`` — 变动比例计算（含零值保护）。
* ``TestDetectYoYChange`` — 同比变动异常触发与边界。
* ``TestDetectQoQChange`` — 环比变动异常。
* ``TestLogicConflicts`` — 4 条科目逻辑冲突规则。
* ``TestSynonyms`` — 科目同义词匹配。
* ``TestSeverity`` — 30%/100% 阈值分级。
* ``TestNoHistoricalData`` — 无对比期返回空。
* ``TestCustomThresholds`` — 自定义阈值生效。
* ``TestImmutability`` — 输入 snapshot 不被修改。
* ``TestIntegrationWithCheckResult`` — 与 ``RuleEngine``/``CheckResult`` 集成。
* ``TestSerialization`` — ``Anomaly.to_dict`` / ``model_dump`` 含 anomalies。

设计要点：

* 同步测试，无 LLM 调用；与 ``test_m3_rule_engine.py`` 风格一致。
* ``_make_snapshot`` 工具函数构造三表快照，便于多期对比。
* 所有数值用 ``Decimal`` 构造，避免 float 精度问题。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.modules.reasoner.anomaly_detector import (
    AnomalyDetector,
    AnomalyType,
)
from app.modules.reasoner.rule_engine import RuleEngine
from app.schemas.reasoning import Severity, StatementSnapshot
from app.schemas.statement import StatementType

# ============================================================================
# 工具函数
# ============================================================================


def _make_snapshot(
    *,
    report_period: str = "2024-12-31",
    bs: dict[str, Decimal] | None = None,
    is_: dict[str, Decimal] | None = None,
    cf: dict[str, Decimal] | None = None,
) -> StatementSnapshot:
    """构造单期三表快照。

    Args:
        report_period: 报告期末日。
        bs: 资产负债表科目 → 值。
        is_: 利润表科目 → 值。
        cf: 现金流量表科目 → 值。

    Returns:
        ``StatementSnapshot`` 实例。
    """
    statements: dict[StatementType, dict[str, Decimal]] = {}
    if bs is not None:
        statements[StatementType.BALANCE_SHEET] = dict(bs)
    if is_ is not None:
        statements[StatementType.INCOME_STATEMENT] = dict(is_)
    if cf is not None:
        statements[StatementType.CASH_FLOW] = dict(cf)
    return StatementSnapshot(
        report_period=report_period,
        statements=statements,
    )


def _find_anomaly(
    anomalies: list[Any],
    *,
    item_name: str | None = None,
    anomaly_type: str | None = None,
) -> Any | None:
    """按 item_name / anomaly_type 过滤首个匹配的 Anomaly。"""
    for a in anomalies:
        if item_name is not None and a.item_name != item_name:
            continue
        if anomaly_type is not None and a.anomaly_type != anomaly_type:
            continue
        return a
    return None


# ============================================================================
# TestAnomalyType
# ============================================================================


class TestAnomalyType:
    """异常类型枚举值与中文名。"""

    def test_enum_values_align_with_schema(self) -> None:
        """anomaly_type 字符串值与 ``Anomaly.anomaly_type`` 列对齐。"""
        assert AnomalyType.YOY_CHANGE.value == "yoy_change"
        assert AnomalyType.QOQ_CHANGE.value == "qoq_change"
        assert AnomalyType.LOGIC_CONFLICT.value == "logic_conflict"

    def test_chinese_names(self) -> None:
        """中文名用于 description 拼接。"""
        assert AnomalyType.YOY_CHANGE.chinese_name == "同比变动"
        assert AnomalyType.QOQ_CHANGE.chinese_name == "环比变动"
        assert AnomalyType.LOGIC_CONFLICT.chinese_name == "科目逻辑异常"


# ============================================================================
# TestChangeRatio
# ============================================================================


class TestChangeRatio:
    """变动比例计算（含零值保护）。"""

    def test_positive_growth(self) -> None:
        """100 → 200 = +1.0（100%）。"""
        ratio = AnomalyDetector._change_ratio(Decimal("200"), Decimal("100"))
        assert ratio == Decimal("1.0")

    def test_decline(self) -> None:
        """100 → 50 = -0.5（-50%）。"""
        ratio = AnomalyDetector._change_ratio(Decimal("50"), Decimal("100"))
        assert ratio == Decimal("-0.5")

    def test_negative_base_value(self) -> None:
        """对比期为负数时，分母用绝对值。"""
        # 净利润 -100 → +100：变动 = 200 / 100 = 2.0（+200%）
        ratio = AnomalyDetector._change_ratio(Decimal("100"), Decimal("-100"))
        assert ratio == Decimal("2.0")

    def test_previous_zero_returns_none(self) -> None:
        """对比期为零返回 None（避免除零）。"""
        assert AnomalyDetector._change_ratio(Decimal("100"), Decimal("0")) is None

    def test_both_zero_returns_none(self) -> None:
        """本期与对比期都为零也返回 None。"""
        assert AnomalyDetector._change_ratio(Decimal("0"), Decimal("0")) is None


# ============================================================================
# TestDetectYoYChange
# ============================================================================


class TestDetectYoYChange:
    """同比变动异常触发与边界。"""

    def test_change_100_percent_triggers_error(self) -> None:
        """plan M3.03 验证用例：100 → 200 触发 ERROR（>= 100%）。"""
        current = _make_snapshot(
            bs={"货币资金": Decimal("200")},
            report_period="2024-12-31",
        )
        year_ago = _make_snapshot(
            bs={"货币资金": Decimal("100")},
            report_period="2023-12-31",
        )

        detector = AnomalyDetector()
        anomalies = detector.detect(current, year_ago=year_ago)

        cash = _find_anomaly(anomalies, item_name="货币资金")
        assert cash is not None
        assert cash.anomaly_type == AnomalyType.YOY_CHANGE.value
        assert cash.severity == Severity.ERROR
        assert cash.metric_value == Decimal("1.0")
        assert cash.threshold == Decimal("0.30")
        assert "同比变动增长 100.0%" in cash.description
        assert "100.00 → 200.00" in cash.description

    def test_change_30_percent_triggers_warn_at_boundary(self) -> None:
        """30% 边界值触发 WARN（|ratio| >= 30%）。"""
        current = _make_snapshot(bs={"货币资金": Decimal("130")})
        year_ago = _make_snapshot(bs={"货币资金": Decimal("100")})

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        cash = _find_anomaly(anomalies, item_name="货币资金")
        assert cash is not None
        assert cash.severity == Severity.WARN
        assert cash.metric_value == Decimal("0.3")

    def test_change_10_percent_does_not_trigger(self) -> None:
        """10% 变动不触发（< 30% 阈值）。"""
        current = _make_snapshot(bs={"货币资金": Decimal("110")})
        year_ago = _make_snapshot(bs={"货币资金": Decimal("100")})

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        assert _find_anomaly(anomalies, item_name="货币资金") is None

    def test_decline_50_percent_triggers_warn(self) -> None:
        """100 → 50 = -50% 触发 WARN（绝对值 50% < 100%）。"""
        current = _make_snapshot(bs={"货币资金": Decimal("50")})
        year_ago = _make_snapshot(bs={"货币资金": Decimal("100")})

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        cash = _find_anomaly(anomalies, item_name="货币资金")
        assert cash is not None
        assert cash.severity == Severity.WARN
        assert cash.metric_value == Decimal("-0.5")
        assert "下滑 50.0%" in cash.description

    def test_decline_100_percent_triggers_error(self) -> None:
        """100 → 0 = -100% 触发 ERROR（绝对值 = 100%）。"""
        current = _make_snapshot(bs={"货币资金": Decimal("0")})
        year_ago = _make_snapshot(bs={"货币资金": Decimal("100")})

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        cash = _find_anomaly(anomalies, item_name="货币资金")
        assert cash is not None
        assert cash.severity == Severity.ERROR

    def test_previous_zero_skipped(self) -> None:
        """对比期值为 0 时跳过（_change_ratio 返回 None）。"""
        current = _make_snapshot(bs={"货币资金": Decimal("100")})
        year_ago = _make_snapshot(bs={"货币资金": Decimal("0")})

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        assert _find_anomaly(anomalies, item_name="货币资金") is None

    def test_multiple_items_detected(self) -> None:
        """多科目同时变动 — 全部检出。"""
        current = _make_snapshot(
            bs={
                "货币资金": Decimal("200"),  # +100% ERROR
                "存货": Decimal("130"),  # +30% WARN
                "应收账款": Decimal("110"),  # +10% 不触发
            }
        )
        year_ago = _make_snapshot(
            bs={
                "货币资金": Decimal("100"),
                "存货": Decimal("100"),
                "应收账款": Decimal("100"),
            }
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        assert len(anomalies) == 2
        cash = _find_anomaly(anomalies, item_name="货币资金")
        inv = _find_anomaly(anomalies, item_name="存货")
        assert cash is not None and cash.severity == Severity.ERROR
        assert inv is not None and inv.severity == Severity.WARN

    def test_item_missing_in_comparison_skipped(self) -> None:
        """对比期无此科目（本期新增）— 跳过。"""
        current = _make_snapshot(
            bs={"货币资金": Decimal("200"), "新增科目": Decimal("999")}
        )
        year_ago = _make_snapshot(bs={"货币资金": Decimal("100")})

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        assert _find_anomaly(anomalies, item_name="新增科目") is None

    def test_across_three_tables(self) -> None:
        """三表独立检测 — BS / IS / CF 各自变动各自触发。"""
        current = _make_snapshot(
            bs={"货币资金": Decimal("200")},
            is_={"营业收入": Decimal("200")},
            cf={"经营活动现金流量净额": Decimal("200")},
        )
        year_ago = _make_snapshot(
            bs={"货币资金": Decimal("100")},
            is_={"营业收入": Decimal("100")},
            cf={"经营活动现金流量净额": Decimal("100")},
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        # 3 个变动异常 + 0 个逻辑异常（同向增长不触发逻辑冲突）。
        change_anomalies = [
            a for a in anomalies if a.anomaly_type == AnomalyType.YOY_CHANGE.value
        ]
        assert len(change_anomalies) == 3


# ============================================================================
# TestDetectQoQChange
# ============================================================================


class TestDetectQoQChange:
    """环比变动异常。"""

    def test_qoq_change_triggers(self) -> None:
        """环比 100 → 200 触发 QOQ_CHANGE。"""
        current = _make_snapshot(bs={"货币资金": Decimal("200")})
        previous = _make_snapshot(bs={"货币资金": Decimal("100")})

        anomalies = AnomalyDetector().detect(current, previous=previous)
        cash = _find_anomaly(anomalies, item_name="货币资金")
        assert cash is not None
        assert cash.anomaly_type == AnomalyType.QOQ_CHANGE.value
        assert cash.severity == Severity.ERROR

    def test_both_yoy_and_qoq_produce_anomalies(self) -> None:
        """同时提供同比和环比 — 各自产出异常（不去重）。"""
        current = _make_snapshot(bs={"货币资金": Decimal("200")})
        previous = _make_snapshot(bs={"货币资金": Decimal("100")})
        year_ago = _make_snapshot(bs={"货币资金": Decimal("100")})

        anomalies = AnomalyDetector().detect(
            current, previous=previous, year_ago=year_ago
        )
        yoy = [a for a in anomalies if a.anomaly_type == AnomalyType.YOY_CHANGE.value]
        qoq = [a for a in anomalies if a.anomaly_type == AnomalyType.QOQ_CHANGE.value]
        assert len(yoy) == 1
        assert len(qoq) == 1


# ============================================================================
# TestLogicConflicts
# ============================================================================


class TestLogicConflicts:
    """4 条科目逻辑冲突规则。"""

    def test_ar_revenue_diverge(self) -> None:
        """应收账款激增 + 营收下滑 — 触发逻辑异常。"""
        current = _make_snapshot(
            bs={"应收账款": Decimal("150")},  # +50%
            is_={"营业收入": Decimal("80")},  # -20%
        )
        year_ago = _make_snapshot(
            bs={"应收账款": Decimal("100")},
            is_={"营业收入": Decimal("100")},
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        logic = _find_anomaly(anomalies, item_name="应收账款激增但营收下滑")
        assert logic is not None
        assert logic.anomaly_type == AnomalyType.LOGIC_CONFLICT.value
        assert logic.severity == Severity.ERROR
        assert "应收账款激增但营收下滑" in logic.description
        assert "ar_ratio=50.0%" in logic.description
        assert "revenue_ratio=-20.0%" in logic.description

    def test_inventory_revenue_diverge(self) -> None:
        """存货激增 + 营收下滑 — 触发逻辑异常。"""
        current = _make_snapshot(
            bs={"存货": Decimal("200")},  # +100%
            is_={"营业收入": Decimal("80")},  # -20%
        )
        year_ago = _make_snapshot(
            bs={"存货": Decimal("100")},
            is_={"营业收入": Decimal("100")},
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        logic = _find_anomaly(anomalies, item_name="存货激增但营收下滑")
        assert logic is not None
        assert logic.severity == Severity.ERROR
        assert "inventory_ratio=100.0%" in logic.description

    def test_revenue_up_net_income_down(self) -> None:
        """营收增长 + 净利润下滑 — 触发逻辑异常。"""
        current = _make_snapshot(
            is_={
                "营业收入": Decimal("150"),  # +50%
                "净利润": Decimal("80"),  # -20%
            }
        )
        year_ago = _make_snapshot(
            is_={
                "营业收入": Decimal("100"),
                "净利润": Decimal("100"),
            }
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        logic = _find_anomaly(anomalies, item_name="营收增长但净利润下滑")
        assert logic is not None
        assert logic.severity == Severity.ERROR
        assert "ni_ratio=-20.0%" in logic.description
        assert "revenue_ratio=50.0%" in logic.description

    def test_ocf_down_net_income_up(self) -> None:
        """净利润增长 + 经营现金流下滑 — 触发盈利质量恶化异常。"""
        current = _make_snapshot(
            is_={"净利润": Decimal("150")},  # +50%
            cf={"经营活动现金流量净额": Decimal("50")},  # -50%
        )
        year_ago = _make_snapshot(
            is_={"净利润": Decimal("100")},
            cf={"经营活动现金流量净额": Decimal("100")},
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        logic = _find_anomaly(anomalies, item_name="净利润增长但经营现金流下滑")
        assert logic is not None
        assert logic.severity == Severity.ERROR
        assert "ocf_ratio=-50.0%" in logic.description
        assert "ni_ratio=50.0%" in logic.description

    def test_no_conflict_when_revenue_grows_with_ar(self) -> None:
        """营收与应收同向增长 — 不触发逻辑异常。"""
        current = _make_snapshot(
            bs={"应收账款": Decimal("150")},  # +50%
            is_={"营业收入": Decimal("150")},  # +50%
        )
        year_ago = _make_snapshot(
            bs={"应收账款": Decimal("100")},
            is_={"营业收入": Decimal("100")},
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        # 应收 +100% 触发变动异常，但不应有应收激增+营收下滑的逻辑异常。
        assert _find_anomaly(anomalies, item_name="应收账款激增但营收下滑") is None

    def test_no_conflict_when_thresholds_not_met(self) -> None:
        """变动幅度未达阈值 — 不触发逻辑异常。"""
        current = _make_snapshot(
            bs={"应收账款": Decimal("115")},  # +15%（< 30%）
            is_={"营业收入": Decimal("85")},  # -15%
        )
        year_ago = _make_snapshot(
            bs={"应收账款": Decimal("100")},
            is_={"营业收入": Decimal("100")},
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        assert _find_anomaly(anomalies, item_name="应收账款激增但营收下滑") is None

    def test_metric_value_takes_first_ratio(self) -> None:
        """逻辑异常 metric_value 取首个 ratio 便于落表排序。"""
        current = _make_snapshot(
            bs={"应收账款": Decimal("150")},  # +50%
            is_={"营业收入": Decimal("80")},  # -20%
        )
        year_ago = _make_snapshot(
            bs={"应收账款": Decimal("100")},
            is_={"营业收入": Decimal("100")},
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        logic = _find_anomaly(anomalies, item_name="应收账款激增但营收下滑")
        assert logic is not None
        assert logic.metric_value == Decimal("0.5")  # ar_ratio
        assert logic.threshold is None  # 逻辑异常无单一阈值


# ============================================================================
# TestSynonyms
# ============================================================================


class TestSynonyms:
    """科目同义词匹配（A 股年报变体）。"""

    def test_revenue_synonym_total_revenue(self) -> None:
        """'营业总收入' 应能与 '营业收入' 互配（同义词组）。"""
        current = _make_snapshot(
            bs={"应收账款": Decimal("150")},
            is_={"营业总收入": Decimal("80")},  # 使用变体名
        )
        year_ago = _make_snapshot(
            bs={"应收账款": Decimal("100")},
            is_={"营业收入": Decimal("100")},  # 本期用变体，对比期用标准名
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        # 应收激增 + 营收下滑的逻辑异常应触发（同义词匹配成功）。
        logic = _find_anomaly(anomalies, item_name="应收账款激增但营收下滑")
        assert logic is not None

    def test_net_income_synonym_parent(self) -> None:
        """'归属于母公司所有者的净利润' 与 '净利润' 互配。"""
        current = _make_snapshot(
            is_={
                "营业收入": Decimal("150"),
                "归属于母公司所有者的净利润": Decimal("80"),  # 变体
            }
        )
        year_ago = _make_snapshot(
            is_={
                "营业收入": Decimal("100"),
                "净利润": Decimal("100"),  # 标准名
            }
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        logic = _find_anomaly(anomalies, item_name="营收增长但净利润下滑")
        assert logic is not None

    def test_ocf_synonym_variant(self) -> None:
        """'经营活动产生的现金流量净额' 变体匹配。"""
        current = _make_snapshot(
            is_={"净利润": Decimal("150")},  # +50%
            cf={"经营活动产生的现金流量净额": Decimal("50")},  # -50% 长名变体
        )
        year_ago = _make_snapshot(
            is_={"净利润": Decimal("100")},
            cf={"经营活动现金流量净额": Decimal("100")},  # 短名变体
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        logic = _find_anomaly(anomalies, item_name="净利润增长但经营现金流下滑")
        assert logic is not None

    def test_missing_revenue_no_logic_anomaly(self) -> None:
        """营收缺失 — 逻辑异常规则全部跳过（无法比较）。"""
        current = _make_snapshot(
            bs={"应收账款": Decimal("150")},  # 应收激增
            # 无利润表
        )
        year_ago = _make_snapshot(
            bs={"应收账款": Decimal("100")},
        )

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        # 应收变动异常会触发，但逻辑异常（需要营收）不触发。
        assert _find_anomaly(anomalies, item_name="应收账款激增但营收下滑") is None
        # 应收 +50% 触发 WARN。
        assert _find_anomaly(anomalies, item_name="应收账款") is not None


# ============================================================================
# TestSeverity
# ============================================================================


class TestSeverity:
    """30%/100% 阈值分级。"""

    def test_warn_at_30_percent(self) -> None:
        """30% 触发 WARN。"""
        current = _make_snapshot(bs={"X": Decimal("130")})
        year_ago = _make_snapshot(bs={"X": Decimal("100")})
        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        assert anomalies[0].severity == Severity.WARN

    def test_error_at_100_percent(self) -> None:
        """100% 触发 ERROR。"""
        current = _make_snapshot(bs={"X": Decimal("200")})
        year_ago = _make_snapshot(bs={"X": Decimal("100")})
        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        assert anomalies[0].severity == Severity.ERROR

    def test_error_at_300_percent(self) -> None:
        """300% 仍是 ERROR。"""
        current = _make_snapshot(bs={"X": Decimal("400")})
        year_ago = _make_snapshot(bs={"X": Decimal("100")})
        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        assert anomalies[0].severity == Severity.ERROR

    def test_logic_anomaly_always_error(self) -> None:
        """逻辑异常 severity 固定为 ERROR。"""
        current = _make_snapshot(
            bs={"应收账款": Decimal("150")},
            is_={"营业收入": Decimal("80")},
        )
        year_ago = _make_snapshot(
            bs={"应收账款": Decimal("100")},
            is_={"营业收入": Decimal("100")},
        )
        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        logic = _find_anomaly(anomalies, item_name="应收账款激增但营收下滑")
        assert logic is not None
        assert logic.severity == Severity.ERROR


# ============================================================================
# TestNoHistoricalData
# ============================================================================


class TestNoHistoricalData:
    """无对比期返回空。"""

    def test_no_previous_no_year_ago_returns_empty(self) -> None:
        """无任何对比期 — 返回空列表。"""
        current = _make_snapshot(bs={"货币资金": Decimal("200")})
        anomalies = AnomalyDetector().detect(current)
        assert anomalies == []

    def test_empty_comparison_snapshot_returns_empty(self) -> None:
        """对比期为空 snapshot（无 statements）— 返回空。"""
        current = _make_snapshot(bs={"货币资金": Decimal("200")})
        empty_year_ago = StatementSnapshot(report_period="2023-12-31")
        anomalies = AnomalyDetector().detect(current, year_ago=empty_year_ago)
        assert anomalies == []


# ============================================================================
# TestCustomThresholds
# ============================================================================


class TestCustomThresholds:
    """自定义阈值生效。"""

    def test_custom_change_threshold_50_percent(self) -> None:
        """阈值放宽到 50% — 30% 不触发，50% 触发。"""
        detector = AnomalyDetector(change_threshold=Decimal("0.50"))

        current_30 = _make_snapshot(bs={"X": Decimal("130")})
        year_ago = _make_snapshot(bs={"X": Decimal("100")})
        assert detector.detect(current_30, year_ago=year_ago) == []

        current_50 = _make_snapshot(bs={"X": Decimal("150")})
        anomalies = detector.detect(current_50, year_ago=year_ago)
        assert len(anomalies) == 1
        assert anomalies[0].threshold == Decimal("0.50")

    def test_custom_large_change_threshold(self) -> None:
        """大幅变动阈值调到 2.0 — 100% 仍是 WARN。"""
        detector = AnomalyDetector(large_change_threshold=Decimal("2.0"))
        current = _make_snapshot(bs={"X": Decimal("200")})  # +100%
        year_ago = _make_snapshot(bs={"X": Decimal("100")})
        anomalies = detector.detect(current, year_ago=year_ago)
        assert anomalies[0].severity == Severity.WARN

    def test_custom_decline_threshold(self) -> None:
        """decline_threshold 调到 -30% — 营收 -20% 不触发逻辑异常。"""
        detector = AnomalyDetector(decline_threshold=Decimal("-0.30"))
        current = _make_snapshot(
            bs={"应收账款": Decimal("150")},  # +50%
            is_={"营业收入": Decimal("80")},  # -20% > -30% 阈值
        )
        year_ago = _make_snapshot(
            bs={"应收账款": Decimal("100")},
            is_={"营业收入": Decimal("100")},
        )
        anomalies = detector.detect(current, year_ago=year_ago)
        assert _find_anomaly(anomalies, item_name="应收账款激增但营收下滑") is None


# ============================================================================
# TestImmutability
# ============================================================================


class TestImmutability:
    """输入 snapshot 不被修改。"""

    def test_input_snapshots_not_mutated(self) -> None:
        """detect 不修改 current / year_ago 的 statements 字典。"""
        current = _make_snapshot(
            bs={"货币资金": Decimal("200")},
            is_={"营业收入": Decimal("80")},
        )
        year_ago = _make_snapshot(
            bs={"货币资金": Decimal("100")},
            is_={"营业收入": Decimal("100")},
        )
        # 深拷贝快照用于事后对比。
        original_current_bs = dict(current.statements[StatementType.BALANCE_SHEET])
        original_year_ago_is = dict(year_ago.statements[StatementType.INCOME_STATEMENT])

        AnomalyDetector().detect(current, year_ago=year_ago)

        assert current.statements[StatementType.BALANCE_SHEET] == original_current_bs
        assert (
            year_ago.statements[StatementType.INCOME_STATEMENT] == original_year_ago_is
        )


# ============================================================================
# TestIntegrationWithCheckResult
# ============================================================================


class TestIntegrationWithCheckResult:
    """与 RuleEngine / CheckResult 集成。"""

    def test_check_result_anomalies_filled(self) -> None:
        """RuleEngine.check → AnomalyDetector.detect → CheckResult.model_copy。"""
        # 构造完整三表（满足规则 1 资产负债恒等式）。
        current = StatementSnapshot(
            report_period="2024-12-31",
            statements={
                StatementType.BALANCE_SHEET: {
                    "资产总计": Decimal("1000"),
                    "负债合计": Decimal("500"),
                    "所有者权益合计": Decimal("500"),
                    "应收账款": Decimal("150"),  # +50%
                },
                StatementType.INCOME_STATEMENT: {
                    "营业收入": Decimal("80"),  # -20%
                    "净利润": Decimal("100"),
                },
                StatementType.CASH_FLOW: {
                    "经营活动现金流量净额": Decimal("100"),
                },
            },
        )
        year_ago = StatementSnapshot(
            report_period="2023-12-31",
            statements={
                StatementType.BALANCE_SHEET: {
                    "资产总计": Decimal("900"),
                    "负债合计": Decimal("500"),
                    "所有者权益合计": Decimal("400"),
                    "应收账款": Decimal("100"),
                },
                StatementType.INCOME_STATEMENT: {
                    "营业收入": Decimal("100"),
                    "净利润": Decimal("100"),
                },
                StatementType.CASH_FLOW: {
                    "经营活动现金流量净额": Decimal("100"),
                },
            },
        )

        # 1. RuleEngine 检查勾稽规则。
        check_result = RuleEngine().check(current)
        assert check_result.anomalies == []

        # 2. AnomalyDetector 检测异常。
        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        assert len(anomalies) > 0

        # 3. model_copy 填充 anomalies。
        updated = check_result.model_copy(update={"anomalies": anomalies})
        assert len(updated.anomalies) == len(anomalies)
        # 原 CheckResult 不变。
        assert check_result.anomalies == []

    def test_confidence_unchanged_after_anomaly_detection(self) -> None:
        """confidence 不被 AnomalyDetector 改动（与 LLMReviewer 一致）。"""
        current = StatementSnapshot(
            report_period="2024-12-31",
            statements={
                StatementType.BALANCE_SHEET: {
                    "资产总计": Decimal("1000"),
                    "负债合计": Decimal("500"),
                    "所有者权益合计": Decimal("500"),
                },
            },
        )
        year_ago = StatementSnapshot(
            report_period="2023-12-31",
            statements={
                StatementType.BALANCE_SHEET: {
                    "资产总计": Decimal("500"),  # +100% ERROR
                },
            },
        )

        check_result = RuleEngine().check(current)
        original_confidence = check_result.confidence
        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        updated = check_result.model_copy(update={"anomalies": anomalies})

        # confidence 不动；调用方（M3.04 编排）可按需调整。
        assert updated.confidence == original_confidence
        assert len(updated.anomalies) > 0

    def test_all_pass_property_with_anomalies(self) -> None:
        """有异常时 all_pass 应为 False。"""
        # 提供完整三表使 3 条勾稽规则全部通过：
        # - 规则 1：资产 1000 = 负债 500 + 权益 500
        # - 规则 2：未分配利润 100 == 净利润 100
        # - 规则 3：OCF 100 == 净利润 100
        current = StatementSnapshot(
            report_period="2024-12-31",
            statements={
                StatementType.BALANCE_SHEET: {
                    "资产总计": Decimal("1000"),
                    "负债合计": Decimal("500"),
                    "所有者权益合计": Decimal("500"),
                    "未分配利润": Decimal("100"),
                    "应收账款": Decimal("150"),  # +50%
                },
                StatementType.INCOME_STATEMENT: {
                    "营业收入": Decimal("80"),  # -20%
                    "净利润": Decimal("100"),
                },
                StatementType.CASH_FLOW: {
                    "经营活动现金流量净额": Decimal("100"),
                },
            },
        )
        year_ago = StatementSnapshot(
            report_period="2023-12-31",
            statements={
                StatementType.BALANCE_SHEET: {
                    "资产总计": Decimal("1000"),
                    "负债合计": Decimal("500"),
                    "所有者权益合计": Decimal("500"),
                    "未分配利润": Decimal("100"),
                    "应收账款": Decimal("100"),
                },
                StatementType.INCOME_STATEMENT: {
                    "营业收入": Decimal("100"),
                    "净利润": Decimal("100"),
                },
                StatementType.CASH_FLOW: {
                    "经营活动现金流量净额": Decimal("100"),
                },
            },
        )

        check_result = RuleEngine().check(current)
        # 规则全过 → all_pass 初值 True。
        assert check_result.all_pass is True

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        updated = check_result.model_copy(update={"anomalies": anomalies})
        assert updated.all_pass is False


# ============================================================================
# TestSerialization
# ============================================================================


class TestSerialization:
    """Anomaly 序列化。"""

    def test_anomaly_model_dump_json(self) -> None:
        """model_dump(mode='json') 含所有字段且 Decimal 序列化为字符串。"""
        current = _make_snapshot(bs={"货币资金": Decimal("200")})
        year_ago = _make_snapshot(bs={"货币资金": Decimal("100")})

        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        dumped = anomalies[0].model_dump(mode="json")

        assert dumped["item_name"] == "货币资金"
        assert dumped["anomaly_type"] == "yoy_change"
        assert dumped["severity"] == "ERROR"
        # Decimal → str in JSON mode；具体格式（"1" vs "1.0"）取决于 Decimal 精度，
        # 用 Decimal 比较避免字符串格式差异。
        assert Decimal(dumped["metric_value"]) == Decimal("1.0")
        assert Decimal(dumped["threshold"]) == Decimal("0.30")
        assert "同比变动增长 100.0%" in dumped["description"]

    def test_check_result_to_dict_includes_anomalies(self) -> None:
        """CheckResult.to_dict 含 anomalies 字段。"""
        current = _make_snapshot(
            bs={"货币资金": Decimal("200")},
        )
        year_ago = _make_snapshot(
            bs={"货币资金": Decimal("100")},
        )

        check_result = RuleEngine().check(current)
        anomalies = AnomalyDetector().detect(current, year_ago=year_ago)
        updated = check_result.model_copy(update={"anomalies": anomalies})

        payload = updated.to_dict()
        assert "anomalies" in payload
        assert len(payload["anomalies"]) == 1
        assert payload["anomalies"][0]["item_name"] == "货币资金"
