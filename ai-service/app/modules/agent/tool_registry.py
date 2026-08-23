"""M5.02 工具注册表（spec §2.3 M9 ToolRegistry）。

每个工具 = ``ToolSpec``（name / description / parameters JSON Schema /
handler）。``execute`` 统一捕获异常 → ``{"ok": False, "error": ...}``，
由 orchestrator 按 ok=False 计数（连续 3 次终止）；业务性"无法计算"
（无数据、知识库未就绪等）返回 ``{"ok": True, "data": null, "reason": ...}``
——不消耗报错计数，Observation 交给 LLM 自行决定换路。

handler 签名统一：``(arguments: dict) -> dict``，返回即 Observation
（调用方字符串化）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    """工具注册项（prompt 渲染 + 执行分派共用）。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler = field(compare=False)


class ToolRegistry:
    """按名注册/执行工具；``specs()`` 供 prompt 渲染。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """注册一个工具（重名覆盖，便于测试注入）。"""
        self._tools[spec.name] = spec

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """执行工具；任何异常都转为 ``ok=False`` 结果（不抛出）。

        Args:
            name: 工具名（不存在时返回错误结果——orchestrator 会先做
                存在性检查，这里是纵深防御）。
            arguments: 工具参数（已通过 parser 的 schema 检查）。

        Returns:
            ``{"ok": bool, "data": ..., "reason": ...}`` 或
            ``{"ok": False, "error": "..."}``。
        """
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "error": f"未知工具: {name!r}"}
        try:
            result = spec.handler(arguments)
        except Exception as error:  # 工具边界：任何异常都是可观测的失败
            return {"ok": False, "error": f"{name} 执行失败: {error}"}
        if not isinstance(result, dict):
            return {"ok": False, "error": f"{name} 返回类型异常: {type(result)!r}"}
        return result

    def specs(self) -> list[dict[str, Any]]:
        """供 prompt 渲染的工具清单（含 JSON Schema）。"""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            }
            for spec in self._tools.values()
        ]

    def known_tools(self) -> set[str]:
        """已注册工具名集合（react_parser 存在性检查用）。"""
        return set(self._tools)

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> str | None:
        """按参数 schema 检查必填项；缺失返回错误描述（首个缺失键）。"""
        spec = self._tools.get(name)
        if spec is None:
            return f"未知工具: {name!r}"
        required = spec.parameters.get("required") or []
        for key in required:
            if key not in arguments:
                return f"缺少必填参数: {key}"
            if arguments[key] is None:
                return f"参数 {key} 不能为 None"
        return None

    def describe(self) -> str:
        """注册表摘要（调试/日志用）。"""
        return json.dumps(
            [{"name": s.name, "params": s.parameters.get("required", [])} for s in self._tools.values()],
            ensure_ascii=False,
        )


__all__ = ["ToolSpec", "ToolRegistry", "ToolHandler"]
