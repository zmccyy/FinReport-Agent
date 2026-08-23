"""M5.02 compute_yoy 工具：科目同比（本期 vs 上期同科目）。"""

from __future__ import annotations

from app.core.mysql_client import StatementReader
from app.modules.agent.tool_registry import ToolSpec
from app.modules.agent.tools._common import (
    find_item,
    ok_data,
    ok_null,
    pct_change,
    to_float,
)


def make_compute_yoy(reader: StatementReader, report_id: int) -> ToolSpec:
    """构建 compute_yoy 工具（同比 = 本期/上期同科目百分比变化）。

    Args:
        reader: 只读查询客户端。
        report_id: 对话绑定的报表 ID。

    Returns:
        ToolSpec；handler 入参 ``{"item"}``。
    """

    def handler(arguments: dict) -> dict:
        item = str(arguments.get("item") or "").strip()
        if not item:
            return ok_null("缺少科目名参数 item")
        statements = reader.fetch_report_statements_by_report_id(report_id)
        if statements is None:
            return ok_null(f"报表不存在（report_id={report_id}）")
        prior = reader.fetch_year_ago_statements(
            statements.company_code, statements.report_period
        )
        current_row = find_item(statements.rows, item, period="本期")
        prior_row = find_item(prior.rows, item, period="本期") if prior else None
        current_value = to_float(current_row.item_value) if current_row else None
        prior_value = to_float(prior_row.item_value) if prior_row else None
        if current_value is None or prior_value is None:
            return ok_null(
                f"科目 {item!r} 同比数据不足（本期/上期缺失）——"
                "上期数据可能未披露或无历史报表"
            )
        yoy = pct_change(current_value, prior_value)
        if yoy is None:
            return ok_null(f"科目 {item!r} 上期值为 0，同比无法计算")
        return ok_data(
            {
                "item": item,
                "yoy_pct": yoy,
                "current_value": current_value,
                "prior_value": prior_value,
                "period_pair": f"{statements.report_period} vs "
                f"{prior.report_period}",
            }
        )

    return ToolSpec(
        name="compute_yoy",
        description="计算科目同比（本期与上期同科目百分比变化）",
        parameters={
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "科目名，如「营业收入」",
                },
            },
            "required": ["item"],
        },
        handler=handler,
    )
