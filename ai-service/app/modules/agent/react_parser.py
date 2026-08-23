"""M5.01 ReAct 输出解析（spec §2.3 M9 + M5.03 json_mode 强约束）。

LLM 输出契约（prompts.py 定义，json_mode 下应直接是 JSON）：

    {"thought": "...", "tool": "query_statement", "arguments": {...}}
    {"thought": "...", "final_answer": "..."}

解析容错（json_mode 偶发失败时兜底，避免一次坏输出杀死整轮对话）：
1. 先按严格 JSON 解析；
2. 失败则提取 ```json ... ``` 代码块；
3. 仍失败则用正则定位 ``"final_answer"`` / ``"tool"`` 键（截取到行尾）。
全失败返回 ``None``——由 orchestrator 追加纠正提示重试（计 1 步），
连续失败达 ``MAX_PARSE_ERRORS`` 次终止。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# 连续输出解析失败上限（避免坏输出无限消耗步数）。
MAX_PARSE_ERRORS = 3


class ReactParseError(ValueError):
    """LLM 输出无法解析为 tool_call 或 final_answer。"""


@dataclass(frozen=True)
class ToolCall:
    """一次工具调用（已通过工具存在性与参数 schema 检查）。"""

    thought: str
    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class FinalAnswer:
    """ReAct 终态回答。"""

    thought: str
    answer: str


def parse_llm_output(text: str, known_tools: set[str]) -> ToolCall | FinalAnswer:
    """解析 LLM 输出为工具调用或最终回答。

    Args:
        text: LLM 输出文本。
        known_tools: 注册工具名集合（未知工具名 → 抛 ``ReactParseError``，
            让 orchestrator 走纠正重试而不是把未知工具当 observation）。

    Returns:
        ``ToolCall`` 或 ``FinalAnswer``。

    Raises:
        ReactParseError: 输出不是合法 JSON、缺必要键、工具名未知、
            arguments 不是对象或值为空。
    """
    payload = _extract_json_object(text)
    if payload is None:
        raise ReactParseError("输出不是合法 JSON 对象")
    if not isinstance(payload, dict):
        raise ReactParseError("JSON 顶层必须是对象")

    thought = str(payload.get("thought") or "").strip()

    if "final_answer" in payload:
        answer = str(payload.get("final_answer") or "").strip()
        if not answer:
            raise ReactParseError("final_answer 为空")
        return FinalAnswer(thought=thought, answer=answer)

    if "tool" in payload:
        tool = str(payload.get("tool") or "").strip()
        if not tool:
            raise ReactParseError("tool 为空")
        if tool not in known_tools:
            raise ReactParseError(f"未知工具: {tool!r}")
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = {}  # 行级容错路径缺 arguments 时按空参处理
        if not isinstance(arguments, dict):
            raise ReactParseError("arguments 必须是 JSON 对象")
        return ToolCall(thought=thought, tool=tool, arguments=arguments)

    raise ReactParseError("输出缺少 final_answer 或 tool 字段")


def _extract_json_object(text: str) -> Any | None:
    """多级容错提取 JSON 对象（严格 → 代码块 → 行级键截取）。"""
    stripped = text.strip()
    # 1. 严格 JSON
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # 2. ```json 代码块
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # 3. 行级键截取：final_answer 或 tool 行（含 thought 行，若存在）
    match = re.search(r'"final_answer"\s*:\s*"((?:[^"\\]|\\.)*)"', stripped)
    if match:
        thought = _line_thought(stripped)
        return {"thought": thought, "final_answer": match.group(1)}
    match = re.search(r'"tool"\s*:\s*"([^"]+)"', stripped)
    if match:
        thought = _line_thought(stripped)
        return {"thought": thought, "tool": match.group(1), "arguments": {}}
    return None


def _line_thought(text: str) -> str:
    """从文本中尽力提取 thought（容错路径专用，缺失返回空串）。"""
    match = re.search(r'"thought"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    return match.group(1) if match else ""


__all__ = [
    "MAX_PARSE_ERRORS",
    "ReactParseError",
    "ToolCall",
    "FinalAnswer",
    "parse_llm_output",
]
