"""M5.01 react_parser 单测：tool_call / final_answer 解析与容错。"""

from __future__ import annotations

import pytest

from app.modules.agent.react_parser import (
    FinalAnswer,
    ReactParseError,
    ToolCall,
    parse_llm_output,
)

KNOWN_TOOLS = {"query_statement", "unit_convert", "search_kb"}


def test_parses_tool_call_json() -> None:
    """标准工具调用 JSON。"""
    output = (
        '{"thought": "需要查货币资金", "tool": "query_statement", '
        '"arguments": {"item": "货币资金"}}'
    )
    parsed = parse_llm_output(output, KNOWN_TOOLS)
    assert isinstance(parsed, ToolCall)
    assert parsed.tool == "query_statement"
    assert parsed.arguments == {"item": "货币资金"}
    assert parsed.thought == "需要查货币资金"


def test_parses_final_answer_json() -> None:
    """标准 final_answer JSON。"""
    output = '{"thought": "信息已足够", "final_answer": "营业收入为 1688 亿元"}'
    parsed = parse_llm_output(output, KNOWN_TOOLS)
    assert isinstance(parsed, FinalAnswer)
    assert parsed.answer == "营业收入为 1688 亿元"


def test_recovers_from_fenced_json_code_block() -> None:
    """json_mode 失效时带 Markdown 代码块仍可解析。"""
    output = '```json\n{"thought": "x", "tool": "unit_convert", "arguments": {"value": 1, "source_unit": "亿元", "target_unit": "万元"}}\n```'
    parsed = parse_llm_output(output, KNOWN_TOOLS)
    assert isinstance(parsed, ToolCall)
    assert parsed.tool == "unit_convert"


def test_recovers_from_final_answer_line_when_not_strict_json() -> None:
    """非严格 JSON（多余文字）时行级提取 final_answer。"""
    output = '好的，我来回答。{"thought": "基于数据", "final_answer": "总资产 3038 亿元"} 结束。'
    parsed = parse_llm_output(output, KNOWN_TOOLS)
    assert isinstance(parsed, FinalAnswer)
    assert "总资产" in parsed.answer


def test_recovers_from_tool_line_when_not_strict_json() -> None:
    """行级提取 tool（arguments 缺失时以空 dict 容错）。"""
    output = '下一步调用工具 {"tool": "search_kb", "thought": "查知识库"}'
    parsed = parse_llm_output(output, KNOWN_TOOLS)
    assert isinstance(parsed, ToolCall)
    assert parsed.tool == "search_kb"
    assert parsed.arguments == {}


@pytest.mark.parametrize(
    "bad_output",
    [
        "不是 JSON 也没有关键键",
        '{"thought": "x", "tool": "no_such_tool", "arguments": {}}',  # 未知工具
        '{"thought": "x", "arguments": {"item": "a"}}',  # 缺 final_answer/tool
        '{"thought": "x", "final_answer": ""}',  # 空回答
        '{"thought": "x", "tool": "query_statement", "arguments": []}',  # 参数非对象
        "[]",  # 顶层非对象
    ],
)
def test_raises_on_unparseable_output(bad_output: str) -> None:
    """坏输出统一抛 ReactParseError（orchestrator 走纠正重试）。"""
    with pytest.raises(ReactParseError):
        parse_llm_output(bad_output, KNOWN_TOOLS)


def test_empty_thought_is_tolerated() -> None:
    """thought 缺失不阻断解析（容错路径）。"""
    output = '{"tool": "unit_convert", "arguments": {"value": 1, "source_unit": "元", "target_unit": "万元"}}'
    parsed = parse_llm_output(output, KNOWN_TOOLS)
    assert isinstance(parsed, ToolCall)
    assert parsed.thought == ""
