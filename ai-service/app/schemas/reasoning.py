"""M8 勾稽与异常 schemas（spec §2.3 M8 + plan M3.01）。

定义勾稽规则引擎的输入 / 输出契约：

* ``RuleType`` — 三条硬编码勾稽规则的枚举。
* ``Severity`` — 规则 / 异常的严重度，对齐 ``accounting_check.severity``
  与 ``anomaly.severity`` 列。
* ``RuleResult`` — 单条规则执行结果，对应 ``accounting_check`` 表一行。
* ``Anomaly`` — 异常检测结果，对应 ``anomaly`` 表一行。
* ``CheckResult`` — Reasoner 整体输出（spec §2.3 M8 L213）。
* ``StatementSnapshot`` — 规则引擎入参，封装三表 + 期间 + 单位，避免规则
  函数直接操作 ``FinancialStatement`` 内部结构（解耦 + 便于单测构造）。

设计要点：

1. **纯数据契约** — 所有模型都是不可变 ``BaseModel``，规则引擎只产出这些
   对象、不持久化；写库由 L2 ``CheckWriter``（M3.04）消费 ``CheckResult``
   后落表，遵循 spec §3.2「L3 算法 / L2 写库」分层。
2. **diff 字段** — spec §2.3 M8 要求 ``diff`` 填充；规则函数计算
   ``actual - expected``，正负号反映"超支 / 短缺"方向。
3. **tolerance** — 浮点比较必须容差（A 股年报单位为元、数值动辄 1e11，
   IEEE754 累积误差不可忽略）；默认 ``1e-2`` 元，可按 unit 调整。
4. **note 字段** — M3.02 LLM 复核勾稽会回填差异原因；M3.01 留空。
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.schemas.statement import FinancialStatement, StatementType


class RuleType(str, Enum):
    """三条硬编码勾稽规则（spec §2.3 M8 L207-209）。"""

    BALANCE_SHEET_IDENTITY = "balance_sheet_identity"
    """资产 = 负债 + 所有者权益"""

    NET_INCOME_TO_RETAINED = "net_income_to_retained"
    """净利润 → 未分配利润变动（含分红调整）"""

    CASH_FLOW_VS_NET_INCOME = "cash_flow_vs_net_income"
    """经营活动现金流净额 vs 净利润差异（含折旧摊销调整）"""

    @property
    def chinese_name(self) -> str:
        """规则中文名（写入 ``accounting_check.rule_name``）。"""
        labels = {
            RuleType.BALANCE_SHEET_IDENTITY: "资产=负债+所有者权益",
            RuleType.NET_INCOME_TO_RETAINED: "净利润→未分配利润变动",
            RuleType.CASH_FLOW_VS_NET_INCOME: "经营现金流 vs 净利润",
        }
        return labels[self]


class Severity(str, Enum):
    """严重度枚举（对齐 ``accounting_check.severity`` / ``anomaly.severity``）。"""

    INFO = "INFO"
    """规则通过 / 无异常"""

    WARN = "WARN"
    """差异超容差但可解释（如分红、折旧）"""

    ERROR = "ERROR"
    """差异超阈值且无法解释，需立即排查"""

    CRITICAL = "CRITICAL"
    """科目缺失 / 数值非法等结构性问题"""


class RuleResult(BaseModel):
    """单条勾稽规则执行结果（对应 ``accounting_check`` 表一行）。

    Attributes:
        rule_type: 规则枚举。
        rule_name: 规则中文名，写入 ``accounting_check.rule_name``。
        expected: 期望值（等式右侧）。
        actual: 实际值（等式左侧）。
        diff: ``actual - expected``；正数表示"实际比期望大"。
        is_pass: ``|diff| <= tolerance`` 时为 True。
        severity: 失败时的严重度；通过时为 INFO。
        tolerance: 浮点比较容差（元）。
        note: 差异原因说明；M3.01 留空，M3.02 LLM 复核回填。
        missing_items: 规则计算时缺失的科目名列表（用于 LLM 复核定位）。
    """

    rule_type: RuleType
    rule_name: str = Field(description="规则中文名，写入 accounting_check.rule_name")
    expected: Decimal | None = Field(default=None, description="期望值（等式右侧）")
    actual: Decimal | None = Field(default=None, description="实际值（等式左侧）")
    diff: Decimal | None = Field(default=None, description="actual - expected")
    is_pass: bool = Field(description="|diff| <= tolerance 时为 True")
    severity: Severity = Field(default=Severity.INFO)
    tolerance: Decimal = Field(default=Decimal("0.01"), description="浮点比较容差（元）")
    note: str = Field(default="", description="差异原因；M3.01 留空，M3.02 LLM 回填")
    missing_items: list[str] = Field(
        default_factory=list,
        description="规则计算时缺失的科目名列表（用于 LLM 复核定位）",
    )

    @field_validator("expected", "actual", "diff", "tolerance")
    @classmethod
    def _coerce_decimal(cls, v: Decimal | None) -> Decimal | None:
        """统一转为 Decimal 避免 float 精度丢失（spec §8.4 数据一致性）。"""
        if v is None:
            return None
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class Anomaly(BaseModel):
    """异常检测结果（对应 ``anomaly`` 表一行）。

    M3.01 只产 RuleResult；Anomaly 由 M3.03 ``AnomalyDetector`` 产出。
    本 schema 提前定义以便 ``CheckResult`` 聚合，避免 M3.03 再改契约。
    """

    item_name: str = Field(description="异常科目名")
    anomaly_type: str = Field(
        description="异常类型：yoy_change / qoq_change / logic_conflict"
    )
    metric_value: Decimal | None = Field(default=None, description="异常指标值")
    threshold: Decimal | None = Field(default=None, description="触发阈值")
    description: str = Field(default="", description="异常描述")
    severity: Severity = Field(default=Severity.WARN)


class StatementSnapshot(BaseModel):
    """规则引擎入参 — 三表快照。

    解耦 ``FinancialStatement`` 与规则实现：规则函数只认 ``StatementSnapshot``，
    便于单测构造 + 支持未来从 L2 DB 查询结果直接构造（M3.04）。

    Attributes:
        report_period: 报告期末日 YYYY-MM-DD。
        currency: 币种 ISO 4217。
        unit: 数值单位（元 / 万元 / 百万元）。
        statements: 按表类型分组的科目列表（item_name → value）。
    """

    report_period: str = Field(description="报告期末日 YYYY-MM-DD")
    currency: str = Field(default="CNY")
    unit: str = Field(default="元")
    statements: dict[StatementType, dict[str, Decimal]] = Field(
        default_factory=dict,
        description="按表类型分组；内层 dict 为 item_name → value",
    )

    @classmethod
    def from_financial_statement(cls, fs: FinancialStatement) -> StatementSnapshot:
        """从 ``FinancialStatement`` 构造快照（合并本期为主）。

        Args:
            fs: Extractor 产出的完整财报 envelope。

        Returns:
            可直接喂给 ``RuleEngine.check`` 的快照。
        """
        snapshot_statements: dict[StatementType, dict[str, Decimal]] = {}
        for st, items in fs.statements.items():
            mapping: dict[str, Decimal] = {}
            for item in items:
                # 同名科目后写覆盖前写；A 股年报同一表不应有同名科目。
                mapping[item.item] = Decimal(str(item.value))
            snapshot_statements[st] = mapping
        return cls(
            report_period=fs.report_period,
            currency=fs.currency,
            unit=fs.unit,
            statements=snapshot_statements,
        )

    def get(
        self, statement_type: StatementType, item_name: str
    ) -> Decimal | None:
        """查询单个科目值；不存在返回 None。"""
        return self.statements.get(statement_type, {}).get(item_name)

    def require(
        self, statement_type: StatementType, synonyms: list[str]
    ) -> tuple[Decimal | None, str | None]:
        """按同义词组查询；任一命中即返回。

        Args:
            statement_type: 目标表类型。
            synonyms: 同义词候选列表，按优先级排序；任一命中即视为找到。

        Returns:
            (命中的科目值, 缺失代表名)。命中时第二个元素为 None；
            全部未命中时第一个元素为 None、第二个元素为 ``synonyms[0]``
            （便于规则函数回填 missing_items 时定位原始请求）。
        """
        table = self.statements.get(statement_type, {})
        for name in synonyms:
            if name in table:
                return table[name], None
        return None, synonyms[0] if synonyms else None


class CheckResult(BaseModel):
    """Reasoner 整体输出（spec §2.3 M8 L213）。

    Attributes:
        rules: 三条规则的执行结果。
        anomalies: 异常检测产物（M3.01 为空）。
        confidence: 整体置信度 0.0-1.0（规则全过 + 无异常 = 1.0）。
        report_period: 报告期末日，便于 L2 落表时关联 report_id。
    """

    rules: list[RuleResult] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    report_period: str = Field(default="")

    @property
    def all_pass(self) -> bool:
        """所有规则通过且无异常。"""
        return all(r.is_pass for r in self.rules) and not self.anomalies

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON-safe dict（L3 → L2 progress payload）。"""
        return {
            "rules": [r.model_dump(mode="json") for r in self.rules],
            "anomalies": [a.model_dump(mode="json") for a in self.anomalies],
            "confidence": self.confidence,
            "report_period": self.report_period,
        }


__all__ = [
    "Anomaly",
    "CheckResult",
    "RuleResult",
    "RuleType",
    "Severity",
    "StatementSnapshot",
]
