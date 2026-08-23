"""M5.02 check_accounting 工具：回读 CHECK 勾稽 + 异常结果。"""

from __future__ import annotations

from app.core.mysql_client import StatementReader
from app.modules.agent.tool_registry import ToolSpec
from app.modules.agent.tools._common import ok_data, ok_null, to_float


def make_check_accounting(reader: StatementReader, report_id: int) -> ToolSpec:
    """构建 check_accounting 工具（读取持久化的勾稽结果）。

    Args:
        reader: 只读查询客户端。
        report_id: 对话绑定的报表 ID。

    Returns:
        ToolSpec；handler 无必填入参。
    """

    def handler(arguments: dict) -> dict:
        result = reader.fetch_check_result(report_id)
        if result is None:
            return ok_null(
                f"勾稽结果未生成（report_id={report_id} 的 CHECK 步骤"
                "未执行或未写入 accounting_check）"
            )
        rules = [
            {
                "rule_name": rule.rule_name,
                "is_pass": rule.is_pass,
                "diff": to_float(rule.diff),
                "note": rule.note,
            }
            for rule in result.rules
        ]
        anomalies = [
            {
                "item_name": anomaly.item_name,
                "anomaly_type": anomaly.anomaly_type,
                "severity": str(anomaly.severity.value),
                "description": anomaly.description,
            }
            for anomaly in result.anomalies
        ]
        return ok_data(
            {
                "rules": rules,
                "anomalies": anomalies,
                "confidence": result.confidence,
            }
        )

    return ToolSpec(
        name="check_accounting",
        description="查看报表勾稽核对结果（规则通过与否、差异、异常检测）",
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=handler,
    )
