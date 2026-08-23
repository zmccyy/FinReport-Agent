"""M5.02 query_statement 工具：按科目/报表/期间查 financial_statement。"""

from __future__ import annotations

from app.core.mysql_client import StatementReader
from app.modules.agent.tool_registry import ToolSpec
from app.modules.agent.tools._common import (
    find_item,
    normalize_statement_type,
    ok_data,
    ok_null,
    to_float,
)


def make_query_statement(reader: StatementReader, report_id: int) -> ToolSpec:
    """构建 query_statement 工具（绑定报表）。

    Args:
        reader: 只读查询客户端（测试注入 fake）。
        report_id: 对话绑定的报表 ID。

    Returns:
        ToolSpec；handler 入参 ``{"item", "report_type", "period"}``。
    """

    def handler(arguments: dict) -> dict:
        statements = reader.fetch_report_statements_by_report_id(report_id)
        if statements is None:
            return ok_null(f"报表不存在（report_id={report_id}）")
        item = str(arguments.get("item") or "").strip()
        if not item:
            return ok_null("缺少科目名参数 item")
        statement_type = normalize_statement_type(
            str(arguments.get("report_type") or "")
        )
        if arguments.get("report_type") and statement_type is None:
            return ok_null(
                f"未知报表类型 {arguments.get('report_type')!r}（可选："
                "balance_sheet/income_statement/cash_flow 或中文名）"
            )
        period = str(arguments.get("period") or "本期")
        row = find_item(
            statements.rows,
            item,
            statement_type=statement_type,
            period=period,
        )
        value = to_float(row.item_value) if row else None
        if row is None or value is None:
            return ok_null(
                f"未找到科目 {item!r}（report_type={statement_type or '任意'}，"
                f"period={period}）——可用科目见 financial_statement，"
                "注意区分合并/母公司与本期/上期"
            )
        return ok_data(
            {
                "item": item,
                "value": value,
                "scope": row.scope,
                "period": row.period_type,
                "unit": statements.unit,
                "report_period": statements.report_period,
                "company_name": statements.company_name,
            }
        )

    return ToolSpec(
        name="query_statement",
        description="按科目名查询财务报表数值（可指定报表类型与期间）",
        parameters={
            "type": "object",
            "properties": {
                "item": {
                    "type": "string",
                    "description": "科目名，如「货币资金」「营业收入」",
                },
                "report_type": {
                    "type": "string",
                    "description": "报表类型：balance_sheet/income_statement/"
                    "cash_flow 或中文（资产负债表/利润表/现金流量表）；缺省任意表",
                },
                "period": {
                    "type": "string",
                    "description": "期间：本期/上期/本年累计/上年同期；缺省本期",
                },
            },
            "required": ["item"],
        },
        handler=handler,
    )
