"""M5.02 unit_convert 工具：金额单位换算（纯计算，无 IO）。"""

from __future__ import annotations

from app.modules.agent.tool_registry import ToolSpec
from app.modules.agent.tools._common import ok_data, ok_null

# 单位 → 元 的基数（A 股财报常见单位）。
_UNIT_BASES: dict[str, float] = {
    "元": 1e0,
    "千元": 1e3,
    "万元": 1e4,
    "百万元": 1e6,
    "千万元": 1e7,
    "亿元": 1e8,
    "亿": 1e8,
}


def make_unit_convert() -> ToolSpec:
    """构建 unit_convert 工具（数值 × 源基数 / 目标基数）。"""

    def handler(arguments: dict) -> dict:
        try:
            value = float(arguments.get("value"))
        except (TypeError, ValueError):
            return ok_null("参数 value 必须是数字")
        source_unit = str(arguments.get("source_unit") or "").strip()
        target_unit = str(arguments.get("target_unit") or "").strip()
        if source_unit not in _UNIT_BASES:
            return ok_null(
                f"未知源单位 {source_unit!r}（可选：{sorted(_UNIT_BASES)}）"
            )
        if target_unit not in _UNIT_BASES:
            return ok_null(
                f"未知目标单位 {target_unit!r}（可选：{sorted(_UNIT_BASES)}）"
            )
        source_base = _UNIT_BASES[source_unit]
        target_base = _UNIT_BASES[target_unit]
        converted = value * source_base / target_base
        return ok_data(
            {
                "value": round(converted, 4),
                "unit": target_unit,
                "source": f"{value} {source_unit}",
            }
        )

    return ToolSpec(
        name="unit_convert",
        description="金额单位换算（元/千元/万元/百万元/千万元/亿元）",
        parameters={
            "type": "object",
            "properties": {
                "value": {"type": "number", "description": "待换算数值"},
                "source_unit": {"type": "string", "description": "源单位，如「万元」"},
                "target_unit": {"type": "string", "description": "目标单位，如「亿元」"},
            },
            "required": ["value", "source_unit", "target_unit"],
        },
        handler=handler,
    )
