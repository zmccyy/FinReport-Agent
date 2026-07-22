"""M8 异常检测（spec §2.3 M8 L210-212 + plan M3.03）。

职责：

* 同比变动异常 — 本期 vs 去年同期，单项变动比例超过阈值（默认 30%）。
* 环比变动异常 — 本期 vs 上期，单项变动比例超过阈值。
* 科目逻辑异常 — 跨科目方向冲突（如应收账款激增但营收下滑）。

设计要点：

1. **同步纯计算** — 与 ``RuleEngine`` 一致，无 IO、不调 LLM；M3.02
   ``LLMReviewer`` 可独立扩展复核逻辑异常（M3.03 不集成，避免过度设计）。
2. **多期快照** — ``StatementSnapshot`` 是单期；``detect`` 接受 ``previous``
   / ``year_ago`` 两个可选对比期，调用方（M3.04 L2 编排）负责从历史报告
   中查询并构造。
3. **科目同义词** — 复用 ``StatementSnapshot.require`` 按同义词组查询，
   容忍 A 股年报科目名变体（"营业收入" vs "营业总收入"）。
4. **Decimal 精度** — 变动比例用 ``Decimal`` 计算，避免 float 累积误差
   （spec §8.4 数据一致性）。
5. **零值保护** — 对比期值为 0 时跳过变动比例计算（除零）；本期与对比期
   均为 0 视为无变动。科目新增/转出（对比期无此科目）的检测由调用方在
   M3.04 编排层另行处理，本检测器只关心"两期均存在且对比期非零"的变动。
6. **不可变输入** — ``detect`` 不修改输入 ``StatementSnapshot``；产出新的
   ``list[Anomaly]``，由调用方 ``model_copy`` 到 ``CheckResult.anomalies``。
7. **confidence 不动** — 与 ``LLMReviewer`` 一致，``AnomalyDetector`` 只
   产出 anomalies，不重新计算 ``CheckResult.confidence``；M3.04 编排层
   可按需调整（避免双重扣分）。
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from app.schemas.reasoning import Anomaly, Severity, StatementSnapshot
from app.schemas.statement import StatementType
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# ============================================================================
# 默认阈值（spec §2.3 M8 L211 "同比/环比变动 > 阈值（默认 30%）"）
# ============================================================================

# 变动比例触发阈值（|ratio| >= CHANGE_THRESHOLD 触发 WARN）。
DEFAULT_CHANGE_THRESHOLD: Decimal = Decimal("0.30")
# 大幅变动阈值（|ratio| >= LARGE_CHANGE_THRESHOLD 升级为 ERROR）。
DEFAULT_LARGE_CHANGE_THRESHOLD: Decimal = Decimal("1.0")
# 逻辑异常中的"反向变化"阈值（如营收下滑 <= DECLINE_THRESHOLD 视为显著下滑）。
DEFAULT_DECLINE_THRESHOLD: Decimal = Decimal("-0.10")


class AnomalyType(str, Enum):
    """异常类型枚举（对齐 ``Anomaly.anomaly_type`` 字符串值）。"""

    YOY_CHANGE = "yoy_change"
    """同比变动异常 — 本期 vs 去年同期。"""

    QOQ_CHANGE = "qoq_change"
    """环比变动异常 — 本期 vs 上一期。"""

    LOGIC_CONFLICT = "logic_conflict"
    """科目逻辑异常 — 跨科目方向冲突（如应收激增但营收下滑）。"""

    @property
    def chinese_name(self) -> str:
        """异常类型中文名（写入 ``anomaly.description`` 前缀）。"""
        labels = {
            AnomalyType.YOY_CHANGE: "同比变动",
            AnomalyType.QOQ_CHANGE: "环比变动",
            AnomalyType.LOGIC_CONFLICT: "科目逻辑异常",
        }
        return labels[self]


# ============================================================================
# 科目同义词组（与 accounting_rules.py 风格一致）
# ============================================================================

REVENUE_SYNONYMS: list[str] = ["营业收入", "营业总收入", "营业收入合计"]
"""营业收入（利润表）。"""

AR_SYNONYMS: list[str] = ["应收账款", "应收账款净额", "应收账款余额"]
"""应收账款（资产负债表）。"""

INVENTORY_SYNONYMS: list[str] = ["存货", "存货净额", "存货余额"]
"""存货（资产负债表）。"""

NET_INCOME_SYNONYMS: list[str] = [
    "净利润",
    "归属母公司股东的净利润",
    "归属于母公司所有者的净利润",
]
"""净利润（利润表）。"""

OCF_SYNONYMS: list[str] = [
    "经营活动产生的现金流量净额",
    "经营活动现金流量净额",
    "经营活动产生的现金流量净额（含金融企业）",
]
"""经营活动现金流净额（现金流量表）。"""


# ============================================================================
# AnomalyDetector
# ============================================================================


class AnomalyDetector:
    """异常检测器 — 基于多期快照识别同比/环比/逻辑异常。

    Attributes:
        change_threshold: 变动比例触发阈值（默认 30%）；``|ratio| >= 阈值``
            触发 WARN。
        large_change_threshold: 大幅变动阈值（默认 100%）；``|ratio| >= 阈值``
            升级为 ERROR。
        decline_threshold: 反向变化阈值（默认 -10%）；逻辑异常中
            ``ratio <= 阈值`` 视为显著下滑。
    """

    def __init__(
        self,
        *,
        change_threshold: Decimal = DEFAULT_CHANGE_THRESHOLD,
        large_change_threshold: Decimal = DEFAULT_LARGE_CHANGE_THRESHOLD,
        decline_threshold: Decimal = DEFAULT_DECLINE_THRESHOLD,
    ) -> None:
        """初始化检测器。

        Args:
            change_threshold: 单项变动触发阈值（默认 30%）。
            large_change_threshold: 大幅变动升级阈值（默认 100%）。
            decline_threshold: 反向变化阈值（默认 -10%）。
        """
        self.change_threshold = change_threshold
        self.large_change_threshold = large_change_threshold
        self.decline_threshold = decline_threshold

    def detect(
        self,
        current: StatementSnapshot,
        *,
        previous: StatementSnapshot | None = None,
        year_ago: StatementSnapshot | None = None,
    ) -> list[Anomaly]:
        """检测异常 — 同比 / 环比 / 逻辑异常。

        优先使用同比数据；若仅提供 ``previous`` 则只用环比。两者都提供时
        各自独立检测，去重留给调用方（M3.04 编排可按 ``item_name`` +
        ``anomaly_type`` 去重）。

        Args:
            current: 本期三表快照。
            previous: 上一期快照（环比）；可选。
            year_ago: 去年同期快照（同比）；可选。

        Returns:
            异常列表；无异常或无对比数据时返回空列表。
        """
        anomalies: list[Anomaly] = []

        if year_ago is not None:
            anomalies.extend(self._detect_changes(current, year_ago, AnomalyType.YOY_CHANGE))
            anomalies.extend(
                self._detect_logic_conflicts(current, year_ago, AnomalyType.YOY_CHANGE)
            )

        if previous is not None:
            anomalies.extend(self._detect_changes(current, previous, AnomalyType.QOQ_CHANGE))
            anomalies.extend(
                self._detect_logic_conflicts(current, previous, AnomalyType.QOQ_CHANGE)
            )

        if anomalies:
            LOGGER.info(
                "[AnomalyDetector.detect] 检出异常 count=%d yoy=%s qoq=%s",
                len(anomalies),
                year_ago is not None,
                previous is not None,
            )
        return anomalies

    # ------------------------------------------------------------------
    # 变动异常检测
    # ------------------------------------------------------------------

    def _detect_changes(
        self,
        current: StatementSnapshot,
        comparison: StatementSnapshot,
        anomaly_type: AnomalyType,
    ) -> list[Anomaly]:
        """遍历本期三表科目，检测变动比例超阈值的项。

        Args:
            current: 本期快照。
            comparison: 对比期快照（同比 or 环比）。
            anomaly_type: ``YOY_CHANGE`` 或 ``QOQ_CHANGE``。

        Returns:
            变动异常列表。
        """
        anomalies: list[Anomaly] = []
        type_label = anomaly_type.chinese_name

        for st_type, current_items in current.statements.items():
            comparison_items = comparison.statements.get(st_type, {})
            for item_name, current_value in current_items.items():
                previous_value = comparison_items.get(item_name)
                if previous_value is None:
                    # 对比期无此科目 — 跳过（可能是本期新增科目，单独规则处理）。
                    continue

                ratio = self._change_ratio(current_value, previous_value)
                if ratio is None:
                    # 对比期为零且本期也为零 — 无变动。
                    continue

                abs_ratio = abs(ratio)
                if abs_ratio < self.change_threshold:
                    continue

                severity = self._severity_for_change(abs_ratio)
                direction = "增长" if ratio > 0 else "下滑"
                description = (
                    f"{item_name}{type_label}{direction} "
                    f"{abs_ratio * 100:.1f}%（{previous_value:.2f} → "
                    f"{current_value:.2f}）"
                )

                anomalies.append(
                    Anomaly(
                        item_name=item_name,
                        anomaly_type=anomaly_type.value,
                        metric_value=ratio,
                        threshold=self.change_threshold,
                        description=description,
                        severity=severity,
                    )
                )

        return anomalies

    @staticmethod
    def _change_ratio(current: Decimal, previous: Decimal) -> Decimal | None:
        """计算变动比例 ``(current - previous) / |previous|``。

        Args:
            current: 本期值。
            previous: 对比期值。

        Returns:
            变动比例；``previous == 0`` 时返回 ``None``（无法计算）。
        """
        if previous == 0:
            return None
        return (current - previous) / abs(previous)

    def _severity_for_change(self, abs_ratio: Decimal) -> Severity:
        """根据变动比例绝对值决定 severity。

        Args:
            abs_ratio: 变动比例绝对值。

        Returns:
            ``WARN``（30%-100%）或 ``ERROR``（>= 100%）。
        """
        if abs_ratio >= self.large_change_threshold:
            return Severity.ERROR
        return Severity.WARN

    # ------------------------------------------------------------------
    # 逻辑异常检测
    # ------------------------------------------------------------------

    def _detect_logic_conflicts(
        self,
        current: StatementSnapshot,
        comparison: StatementSnapshot,
        anomaly_type: AnomalyType,
    ) -> list[Anomaly]:
        """检测跨科目方向冲突（spec §2.3 M8 L212）。

        4 条规则：

        1. **应收账款激增 + 营收下滑** — AR 增长 >= 30% AND 营收下滑 <= -10%
        2. **存货激增 + 营收下滑** — Inventory 增长 >= 30% AND 营收下滑 <= -10%
        3. **净利润下滑 + 营收增长** — NI 下滑 <= -10% AND 营收增长 >= 30%
           （成本失控或毛利率恶化）
        4. **经营现金流下滑 + 净利润增长** — OCF 下滑 <= -30% AND NI 增长 >= 30%
           （盈利质量恶化，可能存在应收/存货堆积）

        Args:
            current: 本期快照。
            comparison: 对比期快照。
            anomaly_type: 异常类型（同比 or 环比）。

        Returns:
            逻辑异常列表。
        """
        anomalies: list[Anomaly] = []

        # 共用：营收同比/环比变动比例。
        revenue_ratio = self._item_ratio(
            current, comparison, StatementType.INCOME_STATEMENT, REVENUE_SYNONYMS
        )
        ar_ratio = self._item_ratio(current, comparison, StatementType.BALANCE_SHEET, AR_SYNONYMS)
        inventory_ratio = self._item_ratio(
            current, comparison, StatementType.BALANCE_SHEET, INVENTORY_SYNONYMS
        )
        ni_ratio = self._item_ratio(
            current, comparison, StatementType.INCOME_STATEMENT, NET_INCOME_SYNONYMS
        )
        ocf_ratio = self._item_ratio(current, comparison, StatementType.CASH_FLOW, OCF_SYNONYMS)

        type_label = anomaly_type.chinese_name

        # 规则 1：应收账款激增 + 营收下滑
        if (
            ar_ratio is not None
            and ar_ratio >= self.change_threshold
            and revenue_ratio is not None
            and revenue_ratio <= self.decline_threshold
        ):
            anomalies.append(
                self._build_logic_anomaly(
                    "应收账款激增但营收下滑",
                    anomaly_type,
                    type_label,
                    ar_ratio=ar_ratio,
                    revenue_ratio=revenue_ratio,
                )
            )

        # 规则 2：存货激增 + 营收下滑
        if (
            inventory_ratio is not None
            and inventory_ratio >= self.change_threshold
            and revenue_ratio is not None
            and revenue_ratio <= self.decline_threshold
        ):
            anomalies.append(
                self._build_logic_anomaly(
                    "存货激增但营收下滑",
                    anomaly_type,
                    type_label,
                    inventory_ratio=inventory_ratio,
                    revenue_ratio=revenue_ratio,
                )
            )

        # 规则 3：净利润下滑 + 营收增长
        if (
            ni_ratio is not None
            and ni_ratio <= self.decline_threshold
            and revenue_ratio is not None
            and revenue_ratio >= self.change_threshold
        ):
            anomalies.append(
                self._build_logic_anomaly(
                    "营收增长但净利润下滑",
                    anomaly_type,
                    type_label,
                    ni_ratio=ni_ratio,
                    revenue_ratio=revenue_ratio,
                )
            )

        # 规则 4：经营现金流下滑 + 净利润增长（盈利质量恶化）
        # OCF 下滑阈值用 change_threshold（30%），更严格于 decline_threshold。
        if (
            ocf_ratio is not None
            and ocf_ratio <= -self.change_threshold
            and ni_ratio is not None
            and ni_ratio >= self.change_threshold
        ):
            anomalies.append(
                self._build_logic_anomaly(
                    "净利润增长但经营现金流下滑",
                    anomaly_type,
                    type_label,
                    ocf_ratio=ocf_ratio,
                    ni_ratio=ni_ratio,
                )
            )

        return anomalies

    def _item_ratio(
        self,
        current: StatementSnapshot,
        comparison: StatementSnapshot,
        st_type: StatementType,
        synonyms: list[str],
    ) -> Decimal | None:
        """查询本期与对比期同名科目并计算变动比例。

        Args:
            current: 本期快照。
            comparison: 对比期快照。
            st_type: 科目所在表类型。
            synonyms: 同义词候选。

        Returns:
            变动比例；任一期缺失或对比期值为零时返回 ``None``。
        """
        current_value, _ = current.require(st_type, synonyms)
        previous_value, _ = comparison.require(st_type, synonyms)
        if current_value is None or previous_value is None:
            return None
        return self._change_ratio(current_value, previous_value)

    @staticmethod
    def _build_logic_anomaly(
        rule_name: str,
        anomaly_type: AnomalyType,
        type_label: str,
        **ratios: Decimal,
    ) -> Anomaly:
        """构造逻辑异常 ``Anomaly``。

        Args:
            rule_name: 规则中文名（如 "应收账款激增但营收下滑"）。
            anomaly_type: 触发时的对比类型（YOY_CHANGE / QOQ_CHANGE），仅用于
                ``type_label`` 拼接；写入 ``Anomaly.anomaly_type`` 的始终是
                ``LOGIC_CONFLICT``。
            type_label: 同比 / 环比中文标签。
            **ratios: 命中规则的变动比例，用于 description 拼接。

        Returns:
            ``Anomaly`` 实例；逻辑异常 ``severity=ERROR``，``metric_value``
            取首个 ratio 便于落表后排序。
        """
        ratio_parts = ", ".join(f"{name}={ratio * 100:.1f}%" for name, ratio in ratios.items())
        description = f"{rule_name}（{type_label}：{ratio_parts}）"
        first_ratio = next(iter(ratios.values()), None)
        return Anomaly(
            item_name=rule_name,
            anomaly_type=AnomalyType.LOGIC_CONFLICT.value,
            metric_value=first_ratio,
            threshold=None,
            description=description,
            severity=Severity.ERROR,
        )


__all__ = [
    "AnomalyDetector",
    "AnomalyType",
    "AR_SYNONYMS",
    "INVENTORY_SYNONYMS",
    "NET_INCOME_SYNONYMS",
    "OCF_SYNONYMS",
    "REVENUE_SYNONYMS",
]
