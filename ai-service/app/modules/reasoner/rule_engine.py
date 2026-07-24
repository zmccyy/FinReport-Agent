"""M8 勾稽规则引擎（spec §2.3 M8 + plan M3.01）。

职责：

* 注册三条硬编码勾稽规则（``BalanceSheetIdentityRule`` /
  ``NetIncomeToRetainedEarningsRule`` / ``CashFlowVsNetIncomeRule``）。
* 顺序执行所有规则，聚合为 ``CheckResult``。
* 计算 ``confidence``：通过规则数 / 总规则数；CRITICAL 失败额外扣 0.2。

设计要点：

* **同步执行** — M3.01 规则是纯计算、无 IO，无需 async；M3.02 LLM 复核
  会引入 async，届时另起 ``AsyncLLMReviewer`` 类，不污染本引擎。
* **规则不抛异常** — 规则内部捕获所有错误转为 ``severity=CRITICAL`` 的
  ``RuleResult``；引擎层不 try/except，保证单条规则异常不阻断其他规则。
* **扩展点** — ``register`` 方法支持注入自定义规则（M3.03 异常检测器
  可作为 ``AnomalyRule`` 注册，统一进入 ``CheckResult.anomalies``）。
"""

from __future__ import annotations

from app.modules.reasoner.accounting_rules import (
    AccountingRule,
    BalanceSheetIdentityRule,
    CashFlowVsNetIncomeRule,
    NetIncomeToRetainedEarningsRule,
)
from app.schemas.reasoning import (
    CheckResult,
    RuleResult,
    RuleType,
    Severity,
    StatementSnapshot,
)
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)


class RuleEngine:
    """勾稽规则引擎 — 顺序执行所有注册的规则。

    Attributes:
        rules: 已注册的规则实例列表，按注册顺序执行。
    """

    def __init__(self) -> None:
        """初始化引擎，注册三条硬编码规则。"""
        self._rules: list[AccountingRule] = [
            BalanceSheetIdentityRule(),
            NetIncomeToRetainedEarningsRule(),
            CashFlowVsNetIncomeRule(),
        ]

    def register(self, rule: AccountingRule) -> None:
        """追加一条自定义规则（M3.03 异常检测器复用）。

        Args:
            rule: 实现 ``AccountingRule`` 协议的实例。
        """
        self._rules.append(rule)
        LOGGER.debug(
            "[RuleEngine] 注册规则 rule_type=%s 总数=%d",
            getattr(rule, "rule_type", "unknown"),
            len(self._rules),
        )

    @property
    def rules(self) -> list[AccountingRule]:
        """返回已注册规则列表（只读视图）。"""
        return list(self._rules)

    def check(self, snapshot: StatementSnapshot) -> CheckResult:
        """执行所有规则并聚合结果。

        Args:
            snapshot: 三表快照（含 report_period / unit / statements）。

        Returns:
            ``CheckResult`` 含所有规则结果 + 整体 confidence。
        """
        results: list[RuleResult] = []
        for rule in self._rules:
            # 提前取出 rule_type 避免异常分支再次访问 rule 失败。
            rule_type = getattr(rule, "rule_type", RuleType.BALANCE_SHEET_IDENTITY)
            try:
                result = rule.check(snapshot)
            except Exception as exc:  # noqa: BLE001 — 规则异常不阻断引擎
                LOGGER.exception("[RuleEngine] 规则执行异常 rule_type=%s", rule_type)
                result = RuleResult(
                    rule_type=rule_type,
                    rule_name=rule_type.chinese_name,
                    is_pass=False,
                    severity=Severity.CRITICAL,
                    note=f"规则执行异常: {exc}",
                )
            results.append(result)

        confidence = self._compute_confidence(results)
        return CheckResult(
            rules=results,
            anomalies=[],  # M3.03 异常检测器填充
            confidence=confidence,
            report_period=snapshot.report_period,
        )

    @staticmethod
    def _compute_confidence(results: list[RuleResult]) -> float:
        """计算整体置信度。

        规则：

        * 基础分 = 通过规则数 / 总规则数
        * 每条 CRITICAL 失败额外扣 0.2（结构性问题更严重）
        * 最低 0.0，最高 1.0

        Args:
            results: 所有规则结果。

        Returns:
            置信度 [0.0, 1.0]。
        """
        if not results:
            return 0.0
        total = len(results)
        passed = sum(1 for r in results if r.is_pass)
        critical_failures = sum(
            1 for r in results if not r.is_pass and r.severity == Severity.CRITICAL
        )
        confidence = passed / total - 0.2 * critical_failures
        return max(0.0, min(1.0, confidence))


__all__ = ["RuleEngine"]
