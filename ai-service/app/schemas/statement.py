"""M7 financial-statement extraction schemas (spec §2.3 M7).

Pydantic models for the JSON contract produced by ``Extractor``:

* ``StatementType`` — three A-share financial statements.
* ``StatementItem`` — one row (item / value / scope / period).
* ``FinancialStatement`` — full extraction envelope (period + currency +
  per-statement item lists).
* ``ExtractionResult`` — ``Extractor.extract`` return value carrying the
  parsed statement on success or the raw model output + error on failure.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class StatementType(str, Enum):
    """A-share annual-report statement categories."""

    BALANCE_SHEET = "balance_sheet"
    INCOME_STATEMENT = "income_statement"
    CASH_FLOW = "cash_flow"

    @property
    def chinese_name(self) -> str:
        """Return the Chinese label used in prompts + reports."""
        labels = {
            StatementType.BALANCE_SHEET: "资产负债表",
            StatementType.INCOME_STATEMENT: "利润表",
            StatementType.CASH_FLOW: "现金流量表",
        }
        return labels[self]


class Scope(str, Enum):
    """Consolidation scope for a statement row."""

    CONSOLIDATED = "合并"
    PARENT_ONLY = "母公司"


class Period(str, Enum):
    """Reporting period tag for a statement row."""

    CURRENT = "本期"
    PRIOR = "上期"
    CURRENT_YEAR_TO_DATE = "本年累计"
    PRIOR_YEAR_TO_DATE = "上年同期"


class StatementItem(BaseModel):
    """One row of a financial statement."""

    item: str = Field(min_length=1, description="科目名称，如「货币资金」")
    value: float = Field(description="数值，单位与 statement.unit 一致；允许负数")
    scope: Scope = Field(default=Scope.CONSOLIDATED, description="合并 / 母公司")
    period: Period = Field(default=Period.CURRENT, description="本期 / 上期 / 累计")

    @field_validator("value")
    @classmethod
    def _reject_nan(cls, v: float) -> float:
        """Reject NaN values (spec §2.3 M7 数值合法性)."""
        import math

        if math.isnan(v):
            raise ValueError("value must not be NaN")
        return v


class FinancialStatement(BaseModel):
    """Full extraction envelope produced by ``Extractor``.

    The ``statements`` dict carries one entry per ``StatementType`` that
    was extracted; absent statements are simply omitted.
    """

    report_period: str = Field(
        min_length=1, description="报告期末日，YYYY-MM-DD，如 2024-12-31"
    )
    currency: str = Field(default="CNY", description="币种 ISO 4217")
    unit: str = Field(default="元", description="数值单位：元 / 万元 / 百万元")
    statements: dict[StatementType, list[StatementItem]] = Field(
        default_factory=dict, description="按表类型分组的科目列表"
    )

    def item_count(self, statement_type: StatementType) -> int:
        """Return the row count for one statement type.

        Args:
            statement_type: Target statement.

        Returns:
            Number of items; 0 when the statement is absent.
        """
        return len(self.statements.get(statement_type, []))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (statement type → string key)."""
        return {
            "report_period": self.report_period,
            "currency": self.currency,
            "unit": self.unit,
            "statements": {
                st.value: [item.model_dump(mode="json") for item in items]
                for st, items in self.statements.items()
            },
        }


class ExtractionResult(BaseModel):
    """``Extractor.extract`` return value.

    Either ``statement`` is populated (success) or ``error`` carries the
    failure reason; ``raw_text`` always carries the raw model output for
    debugging and M2.07 validator retry.
    """

    statement_type: StatementType
    statement: FinancialStatement | None = Field(
        default=None, description="解析成功时的财务报表；失败时为 None"
    )
    raw_text: str = Field(default="", description="模型原始输出文本")
    error: str | None = Field(default=None, description="失败原因；成功时为 None")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0.0, ge=0.0)

    @property
    def success(self) -> bool:
        """Whether extraction produced a parsed statement."""
        return self.statement is not None and self.error is None


__all__ = [
    "ExtractionResult",
    "FinancialStatement",
    "Period",
    "Scope",
    "StatementItem",
    "StatementType",
]
