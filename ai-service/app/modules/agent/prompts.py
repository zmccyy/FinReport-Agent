"""M5.01 ReAct 提示词构建（spec §2.3 M9）。

DeepSeek json_mode 强约束（M5.03 并入）：输出固定为二选一 JSON——

    {"thought": "...", "tool": "工具名", "arguments": {...}}   # 工具调用
    {"thought": "...", "final_answer": "..."}                   # 最终回答

json_mode 协议要求 prompt 中出现 "JSON" 字样（DeepSeek json_object
约束）；system prompt 声明角色与工具清单（JSON Schema 注入），user
消息携带问题 + 逐步历史（thought/action/observation 前缀式拼接，
不使用 messages API——ModelHub.generate 只有 prompt + system_prompt）。
"""

from __future__ import annotations

from typing import Any

MAX_OBSERVATION_CHARS = 1200


def build_system_prompt(tool_specs: list[dict[str, Any]]) -> str:
    """渲染 ReAct system prompt（含工具 JSON Schema）。

    Args:
        tool_specs: ``ToolRegistry.specs()`` 输出（name/description/parameters）。

    Returns:
        System prompt 文本。
    """
    tools_block = "\n".join(
        f"- {spec['name']}: {spec['description']}\n"
        f"  JSON Schema: {_compact_json(spec['parameters'])}"
        for spec in tool_specs
    )
    return (
        "你是一名专业的 A 股财报问答助手，使用 ReAct（思考-行动-观察）方式回答"
        "用户关于财务报表的问题。\n"
        "可用工具：\n"
        f"{tools_block}\n"
        "规则：\n"
        "1. 需要数据时先调用工具获取 Observation，再决定下一步；不要凭空编造数据。\n"
        "2. 每次输出必须是严格 JSON 对象（两种形态之一，禁止其它字段）：\n"
        '   {"thought": "推理过程", "tool": "工具名", "arguments": {...}} —— 调用工具\n'
        '   {"thought": "推理过程", "final_answer": "对用户的最终回答"} —— 结束\n'
        "3. tool 必须是上面清单中的名字；arguments 必须符合该工具的 JSON Schema。\n"
        "4. 得到足够信息后立即输出 final_answer；回答中引用数据时注明来源。\n"
        "5. 工具返回 data 为 null 或 reason 说明不可计算时，换一种方式或直接基于"
        "已有信息回答，不要反复调用同一工具。"
    )


def build_react_prompt(
    question: str,
    history: list[dict[str, str]],
    *,
    company_context: str = "",
) -> str:
    """渲染 user 消息：问题 + 逐步历史（thought/action/observation）。

    Args:
        question: 用户问题。
        history: 已执行步骤，每项含 ``thought`` / ``action`` / ``observation``
            三键（action 为工具调用描述文本）。
        company_context: 公司上下文提示（如「贵州茅台（600519），报告期
            2025-12-31」）；为空则不渲染。

    Returns:
        User prompt 文本。
    """
    parts: list[str] = []
    if company_context:
        parts.append(f"当前报表上下文：{company_context}")
    parts.append(f"用户问题：{question}")
    if history:
        parts.append("\n已执行的步骤（严格遵守其中的观察结果）：")
        for i, step in enumerate(history, start=1):
            parts.append(
                f"[{i}] Thought: {step['thought']}\n"
                f"    Action: {step['action']}\n"
                f"    Observation: {step['observation']}"
            )
    parts.append("\n请输出下一步（工具调用或 final_answer）的 JSON。")
    return "\n".join(parts)


def build_retry_prompt(error_hint: str) -> str:
    """输出解析失败时的纠正提示（作为下一步 system 追加）。

    Args:
        error_hint: 解析错误说明（如 ``Expecting ','``）。

    Returns:
        追加到 prompt 的纠正指令。
    """
    return (
        f"\n注意：上一次输出 JSON 解析失败（{error_hint}）。"
        "请只输出严格合法的 JSON 对象，不要包含注释、Markdown 代码块或多余文字。\n"
    )


def _compact_json(value: dict[str, Any]) -> str:
    """JSON 压缩为单行（省略空 schema 的必填字段细节，控制 prompt 体积）。"""
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "build_system_prompt",
    "build_react_prompt",
    "build_retry_prompt",
    "MAX_OBSERVATION_CHARS",
]
