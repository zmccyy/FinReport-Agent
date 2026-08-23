"""M5.02 compute_qoq 工具：科目环比（季度/半年度序列）。

A 股年报仅披露本期/上期（同比）两期，无季度序列——环比（qoq）在
年报上下文不可计算。工具按 spec 表注册，但业务性返回 ``ok=True +
data=null + reason``（不消耗工具报错计数），LLM 看到 reason 后应转用
``compute_yoy`` 或直接回答。季度/半年度报告接入后，此处可扩展为按
period 序列计算。
"""

from __future__ import annotations

from app.core.mysql_client import StatementReader
from app.modules.agent.tool_registry import ToolSpec
from app.modules.agent.tools._common import ok_null


def make_compute_qoq(reader: StatementReader, report_id: int) -> ToolSpec:
    """构建 compute_qoq 工具（当前：年报场景明确不可计算）。"""

    def handler(arguments: dict) -> dict:
        item = str(arguments.get("item") or "").strip()
        if not item:
            return ok_null("缺少科目名参数 item")
        statements = reader.fetch_report_statements_by_report_id(report_id)
        if statements is None:
            return ok_null(f"报表不存在（report_id={report_id}）")
        period_set = {row.period_type for row in statements.rows if row.item_name == item}
        if not period_set:
            return ok_null(f"未找到科目 {item!r}")
        if period_set == {"本期", "上期"} or period_set == {"本期"}:
            return ok_null(
                f"年度报表仅有 {sorted(period_set)} 两期（同比）数据，"
                "环比（季度/半年度）序列不可得；如需同比请用 compute_yoy"
            )
        periods = sorted(period_set)
        return ok_null(
            f"科目 {item!r} 的期间序列为 {periods}，环比计算需季度粒度数据，"
            "当前报表无季度序列"
        )

    return ToolSpec(
        name="compute_qoq",
        description="计算科目环比（季度/半年度序列）；年度报表通常不可计算",
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
