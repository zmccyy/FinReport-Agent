"""M5.02 工具共享辅助：科目行查找 / 百分比变化 / 结果包装。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.core.mysql_client import StatementRow

# 报表类型入参（中文/英文均可）→ financial_statement.statement_type。
_STATEMENT_TYPE_MAP = {
    "balance_sheet": "balance_sheet",
    "资产负债表": "balance_sheet",
    "bs": "balance_sheet",
    "income_statement": "income_statement",
    "利润表": "income_statement",
    "is": "income_statement",
    "cash_flow": "cash_flow",
    "现金流量表": "cash_flow",
    "cf": "cash_flow",
}


def normalize_statement_type(raw: str) -> str | None:
    """报表类型入参规范化；未知返回 None。"""
    return _STATEMENT_TYPE_MAP.get(str(raw).strip().lower())


def find_item(
    rows: tuple[StatementRow, ...] | list[StatementRow],
    item: str,
    *,
    statement_type: str | None = None,
    period: str = "本期",
    scope: str = "合并",
) -> StatementRow | None:
    """按科目名 + 粒度（默认合并/本期）查找行；找不到返回 None。"""
    item = item.strip()
    for row in rows:
        if row.item_name != item:
            continue
        if statement_type is not None and row.statement_type != statement_type:
            continue
        if period and row.period_type != period:
            continue
        if scope and row.scope != scope:
            continue
        return row
    return None


def to_float(value: Decimal | float | None) -> float | None:
    """Decimal → float（None/NaN 保持 None）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct_change(current: float, prior: float) -> float | None:
    """同比/环比百分比：``(current - prior) / |prior| * 100``。

    Args:
        current: 本期值。
        prior: 对比期值（分母）。

    Returns:
        百分比（保留 2 位）；prior 为 0 时返回 None（除零无意义）。
    """
    if prior == 0:
        return None
    return round((current - prior) / abs(prior) * 100, 2)


def ok_data(data: Any) -> dict[str, Any]:
    """工具成功结果包装。"""
    return {"ok": True, "data": data}


def ok_null(reason: str) -> dict[str, Any]:
    """业务性"无法计算/无数据"（不消耗工具报错计数，LLM 可换路）。"""
    return {"ok": True, "data": None, "reason": reason}


__all__ = [
    "normalize_statement_type",
    "find_item",
    "to_float",
    "pct_change",
    "ok_data",
    "ok_null",
]
