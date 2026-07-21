"""M2.09 extract handler — emits M2.09 contract payload (M2.06 仍 mock).

This handler is invoked by ``app.mq.consumer`` for routing keys
``extract.bs`` / ``extract.is`` / ``extract.cf``. The fully wired M2.09+
path (M4 T1 1.5B QLoRA / Qwen2.5-7B) will replace the mock body with::

    with VramScheduler.load_for_scene_with_lock(Scene.REASON) as _lock:
        result, validation = extract_with_retry(extractor, validator, table_html, st, ...)
    return _build_payload(step, result, validation, ...)

The mock below returns a deterministic payload that already matches the
M2.09 contract consumed by L2 ``StatementWriter``: the BS step ships two
rows, the IS / CF steps ship one row each, so the L2 writer can be
exercised end-to-end without the 7B model loaded.
"""

from typing import Any

from app.schemas.task import TaskMessage

# step name → L3 StatementType.value (kept as raw strings to avoid
# importing app.schemas.statement here; L2 locks the same string values).
_STEP_TO_TYPE = {
    "extract.bs": "balance_sheet",
    "extract.is": "income_statement",
    "extract.cf": "cash_flow",
}

# Mock rows for the M2.09 contract. Each row mirrors the L3
# ``StatementItem`` shape: ``item`` / ``value`` / ``scope`` / ``period``.
_MOCK_ITEMS: dict[str, list[dict[str, Any]]] = {
    "balance_sheet": [
        {"item": "货币资金", "value": 1.23e9, "scope": "合并", "period": "本期"},
        {"item": "资产总计", "value": 5.67e10, "scope": "合并", "period": "本期"},
    ],
    "income_statement": [
        {"item": "营业收入", "value": 8.9e9, "scope": "合并", "period": "本期"},
    ],
    "cash_flow": [
        {
            "item": "经营活动产生的现金流量净额",
            "value": 1.2e9,
            "scope": "合并",
            "period": "本期",
        },
    ],
}


async def handle(message: TaskMessage) -> dict[str, Any]:
    """Emit a deterministic M2.09 contract payload.

    Args:
        message: Validated extract task message; ``message.step`` is one
            of ``extract.bs`` / ``extract.is`` / ``extract.cf``.

    Returns:
        Dict matching the M2.09 contract consumed by L2 ``StatementWriter``::

            {
              "success": True,
              "statement": {
                "report_period": "2024-12-31",
                "currency": "CNY",
                "unit": "元",
                "statements": {
                  "balance_sheet": [
                    {"item": "货币资金", "value": 1.23e9, "scope": "合并", "period": "本期"},
                    ...
                  ]
                }
              },
              "validation": {"is_valid": True, "issues": [], "error_hint": ""},
              "confidence": 0.92,
              "source_page": 5,
              "retried": False,
              "tokens_used": 1234,
              "latency_ms": 5600
            }
    """
    statement_type = _STEP_TO_TYPE.get(message.step)
    if statement_type is None:
        # M2 review fix: 之前对未知 step 静默 fallback 到 balance_sheet 并返回 success=True,
        # 若 MQ 路由配置错误把 extract.xyz 投进来,会写入 balance_sheet 假数据且无告警。
        # 改为显式报错,让 MQ consumer 走 DLQ,避免污染数据。
        raise ValueError(
            f"Unknown extract step: {message.step!r}, "
            f"expected one of {sorted(_STEP_TO_TYPE.keys())}"
        )
    items = _MOCK_ITEMS.get(statement_type, [])
    return {
        "success": True,
        "statement": {
            "report_period": "2024-12-31",
            "currency": "CNY",
            "unit": "元",
            "statements": {statement_type: items},
        },
        "validation": {"is_valid": True, "issues": [], "error_hint": ""},
        "confidence": 0.92,
        "source_page": 5,
        "retried": False,
        "tokens_used": 1234,
        "latency_ms": 5600,
    }
