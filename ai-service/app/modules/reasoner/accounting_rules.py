"""M8 勾稽规则实现（spec §2.3 M8 L207-209 + plan M3.01）。

三条硬编码勾稽规则：

1. **资产负债恒等式** — ``资产总计 = 负债合计 + 所有者权益合计``
2. **净利润→未分配利润变动** —
   ``未分配利润期末 = 未分配利润期初 + 净利润 - 提取盈余公积 - 分红``
   （A 股年报通常不直接披露分红金额，缺分红科目时容差放宽到净利润的 5%）
3. **经营现金流 vs 净利润** —
   ``经营现金流净额 ≈ 净利润 + 折旧摊销 + 营运资本变动``
   （无折旧摊销科目时，用净利润 ±20% 作为粗容差）

设计原则：

* **纯函数** — 规则函数无副作用、不调 LLM、不查 DB；输入 ``StatementSnapshot``
  输出 ``RuleResult``。便于单测 + 在 L3 多 worker 并行执行。
* **科目名同义词** — A 股年报科目名存在变体（"资产总计" vs "资产合计"），
  每个规则定义 ``SYNONYMS`` 列表，按顺序匹配第一个命中。
* **Decimal 精度** — 所有数值用 ``decimal.Decimal`` 计算，避免 float 累积误差
  （spec §8.4 数据一致性）。
* **缺失科目降级** — 必需科目缺失时返回 ``is_pass=False, severity=CRITICAL``，
  ``missing_items`` 回填缺失名，便于 M3.02 LLM 复核定位。
* **容差分级** — 默认 ``1e-2`` 元（absolute），规则 2/3 在缺调整项时放宽到
  ``relative_tolerance``（净利润的 5% / 20%）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from app.schemas.reasoning import (
    RuleResult,
    RuleType,
    Severity,
    StatementSnapshot,
)
from app.schemas.statement import StatementType
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# 默认绝对容差（元）。A 股年报单位为元、数值动辄 1e11，但 IEEE754 在 Decimal
# 下精确，1e-2 已足够覆盖四舍五入误差。
DEFAULT_TOLERANCE: Decimal = Decimal("0.01")


class AccountingRule(Protocol):
    """勾稽规则协议 — 规则函数签名约束。"""

    rule_type: RuleType
    """规则枚举（类属性，便于 ``RuleEngine`` 注册）。"""

    def check(self, snapshot: StatementSnapshot) -> RuleResult:
        """执行规则并返回结果。"""
        ...


# ============================================================================
# 规则 1：资产 = 负债 + 所有者权益
# ============================================================================


class BalanceSheetIdentityRule:
    """资产负债恒等式（spec §2.3 M8 L207）。

    等式：``资产总计 = 负债合计 + 所有者权益合计``

    A 股年报科目名变体：

    * 资产总计 / 资产合计
    * 负债合计 / 负债总计
    * 所有者权益合计 / 股东权益合计 / 净资产合计
    """

    rule_type = RuleType.BALANCE_SHEET_IDENTITY

    ASSET_SYNONYMS: list[str] = ["资产总计", "资产合计"]
    LIABILITY_SYNONYMS: list[str] = ["负债合计", "负债总计"]
    EQUITY_SYNONYMS: list[str] = [
        "所有者权益合计",
        "股东权益合计",
        "净资产合计",
        "所有者权益（或股东权益）合计",
    ]

    def check(self, snapshot: StatementSnapshot) -> RuleResult:
        """执行资产负债恒等式检查。"""
        asset, asset_missing_name = snapshot.require(
            StatementType.BALANCE_SHEET, self.ASSET_SYNONYMS
        )
        liability, liability_missing_name = snapshot.require(
            StatementType.BALANCE_SHEET, self.LIABILITY_SYNONYMS
        )
        equity, equity_missing_name = snapshot.require(
            StatementType.BALANCE_SHEET, self.EQUITY_SYNONYMS
        )

        all_missing = [
            name
            for name in (
                asset_missing_name,
                liability_missing_name,
                equity_missing_name,
            )
            if name
        ]
        if all_missing:
            return RuleResult(
                rule_type=self.rule_type,
                rule_name=self.rule_type.chinese_name,
                is_pass=False,
                severity=Severity.CRITICAL,
                note=f"缺失必需科目: {', '.join(all_missing)}",
                missing_items=all_missing,
            )

        # 三个必需科目都已命中，类型 narrowed 为 Decimal。
        assert asset is not None
        assert liability is not None
        assert equity is not None

        expected = liability + equity
        actual = asset
        diff = actual - expected
        is_pass = abs(diff) <= DEFAULT_TOLERANCE
        severity = Severity.INFO if is_pass else Severity.ERROR

        return RuleResult(
            rule_type=self.rule_type,
            rule_name=self.rule_type.chinese_name,
            expected=expected,
            actual=actual,
            diff=diff,
            is_pass=is_pass,
            severity=severity,
            tolerance=DEFAULT_TOLERANCE,
            note="" if is_pass else "资产负债恒等式不成立，需排查科目分类错误",
        )


# ============================================================================
# 规则 2：净利润 → 未分配利润变动
# ============================================================================


class NetIncomeToRetainedEarningsRule:
    """净利润→未分配利润变动（spec §2.3 M8 L208）。

    等式：
    ``未分配利润期末 = 未分配利润期初 + 净利润 - 提取盈余公积 - 应付股利``

    A 股年报限制：

    * 未分配利润通常只有期末数（期初需从上期对比数据获取，M3.01 不依赖上期）
    * 盈余公积、应付股利在 BS 中可能未单独披露
    * 缺调整项时容差放宽到 ``净利润 × 5%``（典型盈余公积 + 分红占净利润比例）
    * 缺未分配利润或净利润时直接 CRITICAL 失败
    """

    rule_type = RuleType.NET_INCOME_TO_RETAINED

    RETAINED_SYNONYMS: list[str] = [
        "未分配利润",
        "未分配利润（或未弥补亏损）",
        "归属母公司所有者权益-未分配利润",
    ]
    NET_INCOME_SYNONYMS: list[str] = [
        "净利润",
        "归属母公司股东的净利润",
        "归属于母公司所有者的净利润",
    ]
    SURPLUS_SYNONYMS: list[str] = ["提取盈余公积", "盈余公积增加"]
    DIVIDEND_SYNONYMS: list[str] = ["应付股利", "应付普通股股利", "现金股利"]

    # 缺调整项时的相对容差（占净利润的比例）。
    RELATIVE_TOLERANCE: Decimal = Decimal("0.05")

    def check(self, snapshot: StatementSnapshot) -> RuleResult:
        """执行净利润→未分配利润变动检查。"""
        retained, retained_missing_name = snapshot.require(
            StatementType.BALANCE_SHEET, self.RETAINED_SYNONYMS
        )
        net_income, income_missing_name = snapshot.require(
            StatementType.INCOME_STATEMENT, self.NET_INCOME_SYNONYMS
        )

        missing = [
            name for name in (retained_missing_name, income_missing_name) if name
        ]
        if missing:
            return RuleResult(
                rule_type=self.rule_type,
                rule_name=self.rule_type.chinese_name,
                is_pass=False,
                severity=Severity.CRITICAL,
                note=f"缺失必需科目: {', '.join(missing)}",
                missing_items=missing,
            )

        assert retained is not None
        assert net_income is not None

        # 可选调整项（任一命中即累加；缺失时容差放宽）。
        surplus, _ = snapshot.require(
            StatementType.BALANCE_SHEET, self.SURPLUS_SYNONYMS
        )
        dividend, _ = snapshot.require(
            StatementType.BALANCE_SHEET, self.DIVIDEND_SYNONYMS
        )
        adjustments = (surplus or Decimal("0")) + (dividend or Decimal("0"))

        expected = net_income - adjustments
        actual = retained
        diff = actual - expected

        # 容差：有调整项用绝对容差；无调整项放宽到净利润的 5%。
        if adjustments != 0:
            tolerance = DEFAULT_TOLERANCE
        else:
            tolerance = max(
                DEFAULT_TOLERANCE,
                abs(net_income) * self.RELATIVE_TOLERANCE,
            )

        is_pass = abs(diff) <= tolerance
        severity = Severity.INFO if is_pass else Severity.WARN

        note = ""
        if not is_pass:
            if adjustments == 0:
                note = "缺盈余公积/应付股利科目，容差放宽到净利润的 5%；差异可能来自分红或盈余公积计提"
            else:
                note = "未分配利润变动与净利润不匹配，需排查分红/盈余公积披露"

        return RuleResult(
            rule_type=self.rule_type,
            rule_name=self.rule_type.chinese_name,
            expected=expected,
            actual=actual,
            diff=diff,
            is_pass=is_pass,
            severity=severity,
            tolerance=tolerance,
            note=note,
        )


# ============================================================================
# 规则 3：经营活动现金流净额 vs 净利润
# ============================================================================


class CashFlowVsNetIncomeRule:
    """经营活动现金流净额 vs 净利润差异（spec §2.3 M8 L209）。

    等式：
    ``经营现金流净额 ≈ 净利润 + 折旧摊销 + 营运资本变动``

    A 股年报限制：

    * 折旧摊销通常在 CF 附注中披露，主表可能无此科目
    * 营运资本变动计算复杂，M3.01 不实现
    * 无折旧摊销时容差放宽到 ``|净利润| × 20%``（典型折旧占净利润 10-30%）
    """

    rule_type = RuleType.CASH_FLOW_VS_NET_INCOME

    OCF_SYNONYMS: list[str] = [
        "经营活动产生的现金流量净额",
        "经营活动现金流量净额",
        "经营活动产生的现金流量净额（含金融企业）",
    ]
    NET_INCOME_SYNONYMS: list[str] = [
        "净利润",
        "归属母公司股东的净利润",
        "归属于母公司所有者的净利润",
    ]
    DEPRECIATION_SYNONYMS: list[str] = [
        "固定资产折旧",
        "折旧摊销",
        "固定资产折旧、油气资产折耗、生产性生物资产折旧",
        "资产减值准备",
    ]

    # 缺折旧摊销时的相对容差（占净利润的比例）。
    RELATIVE_TOLERANCE: Decimal = Decimal("0.20")

    def check(self, snapshot: StatementSnapshot) -> RuleResult:
        """执行经营现金流 vs 净利润检查。"""
        ocf, ocf_missing_name = snapshot.require(
            StatementType.CASH_FLOW, self.OCF_SYNONYMS
        )
        net_income, income_missing_name = snapshot.require(
            StatementType.INCOME_STATEMENT, self.NET_INCOME_SYNONYMS
        )

        missing = [name for name in (ocf_missing_name, income_missing_name) if name]
        if missing:
            return RuleResult(
                rule_type=self.rule_type,
                rule_name=self.rule_type.chinese_name,
                is_pass=False,
                severity=Severity.CRITICAL,
                note=f"缺失必需科目: {', '.join(missing)}",
                missing_items=missing,
            )

        assert ocf is not None
        assert net_income is not None

        # 可选折旧摊销（任一命中即累加；缺失时容差放宽）。
        depreciation, _ = snapshot.require(
            StatementType.CASH_FLOW, self.DEPRECIATION_SYNONYMS
        )
        depreciation = depreciation or Decimal("0")

        expected = net_income + depreciation
        actual = ocf
        diff = actual - expected

        if depreciation != 0:
            tolerance = DEFAULT_TOLERANCE
        else:
            tolerance = max(
                DEFAULT_TOLERANCE,
                abs(net_income) * self.RELATIVE_TOLERANCE,
            )

        is_pass = abs(diff) <= tolerance
        severity = Severity.INFO if is_pass else Severity.WARN

        note = ""
        if not is_pass:
            if depreciation == 0:
                note = "缺折旧摊销科目，容差放宽到净利润的 20%；差异可能来自折旧摊销或营运资本变动"
            else:
                note = "经营现金流与净利润差异超容差，需排查营运资本变动"

        return RuleResult(
            rule_type=self.rule_type,
            rule_name=self.rule_type.chinese_name,
            expected=expected,
            actual=actual,
            diff=diff,
            is_pass=is_pass,
            severity=severity,
            tolerance=tolerance,
            note=note,
        )


__all__ = [
    "AccountingRule",
    "BalanceSheetIdentityRule",
    "CashFlowVsNetIncomeRule",
    "NetIncomeToRetainedEarningsRule",
]
