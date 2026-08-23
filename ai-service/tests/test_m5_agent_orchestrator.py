"""M5.01 AgentOrchestrator 单测：ReAct 循环、终止条件、工具错误处理。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from app.modules.agent.orchestrator import (
    FINISHED_GENERATION_ERROR,
    FINISHED_MAX_STEPS,
    FINISHED_PARSE_ERROR,
    FINISHED_TOO_MANY_TOOL_ERRORS,
    AgentOrchestrator,
)
from app.modules.agent.tool_registry import ToolRegistry, ToolSpec


class ScriptedHub:
    """脚本化 ModelHub：按序返回预设输出；耗尽后报错。"""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[dict] = []
        self.generation = SimpleNamespace(text="")

    def generate(self, prompt: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({"prompt": prompt, **kwargs})
        if not self.outputs:
            raise RuntimeError("script exhausted")
        text = self.outputs.pop(0)
        return SimpleNamespace(text=text)


def _make_registry(
    *,
    echo: dict | None = None,
    fail_times: int = 0,
    schema_required: list[str] | None = None,
) -> ToolRegistry:
    """注册一个脚本化工具（默认返回 echo 数据）。"""

    state = {"fails": 0}

    def handler(arguments: dict) -> dict:
        if state["fails"] < fail_times:
            state["fails"] += 1
            raise ValueError(f"模拟失败 {state['fails']}")
        return {"ok": True, "data": echo or arguments}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="demo_tool",
            description="测试工具",
            parameters={
                "type": "object",
                "properties": {"item": {"type": "string"}},
                "required": schema_required or [],
            },
            handler=handler,
        )
    )
    return registry


def _tool_output(thought: str = "思考") -> str:
    return json.dumps(
        {"thought": thought, "tool": "demo_tool", "arguments": {"item": "货币资金"}},
        ensure_ascii=False,
    )


def _final_output(answer: str = "最终答案") -> str:
    return json.dumps({"thought": "思考完毕", "final_answer": answer}, ensure_ascii=False)


def test_single_tool_then_final_answer() -> None:
    """工具调用 → Observation → final_answer 正常结束。"""
    hub = ScriptedHub([_tool_output(), _final_output("营业收入 1688 亿")])
    orchestrator = AgentOrchestrator(hub, _make_registry())
    result = orchestrator.run("营收多少？", company_context="贵州茅台")
    assert result.success is True
    assert result.answer == "营业收入 1688 亿"
    assert result.total_steps == 2
    assert len(result.steps) == 1
    assert result.steps[0].action.startswith("demo_tool(")
    # json_mode + system_prompt 已注入
    assert hub.calls[0]["json_mode"] is True
    assert "demo_tool" in hub.calls[0]["system_prompt"]


def test_direct_final_answer_no_tool() -> None:
    """问题无需工具直接回答。"""
    hub = ScriptedHub([_final_output("直接回答")])
    result = AgentOrchestrator(hub, _make_registry()).run("简单问题")
    assert result.success is True
    assert result.total_steps == 1
    assert result.steps == []


def test_tool_error_three_times_terminates() -> None:
    """工具连续报错 3 次终止（spec M9 终止条件）。"""
    hub = ScriptedHub([_tool_output()] * 3 + [_final_output()])
    result = AgentOrchestrator(hub, _make_registry(fail_times=99)).run("问题")
    assert result.success is False
    assert result.finished_reason == FINISHED_TOO_MANY_TOOL_ERRORS
    assert result.tool_errors == 3
    assert result.error == "连续 3 次工具报错"


def test_max_steps_terminates() -> None:
    """步数上限 8 终止（只输出工具调用）。"""
    hub = ScriptedHub([_tool_output()] * 8)
    result = AgentOrchestrator(hub, _make_registry()).run("问题")
    assert result.success is False
    assert result.finished_reason == FINISHED_MAX_STEPS
    assert result.total_steps == 8


def test_parse_error_recovers_with_retry_hint() -> None:
    """一次坏输出 → 追加纠正提示重试 → 成功。"""
    hub = ScriptedHub(["这不是 JSON", _final_output("恢复成功")])
    result = AgentOrchestrator(hub, _make_registry()).run("问题")
    assert result.success is True
    assert result.parse_errors == 1
    assert result.total_steps == 2
    assert "解析失败" in hub.calls[1]["prompt"]


def test_parse_error_three_times_terminates() -> None:
    """连续 3 次解析失败终止。"""
    hub = ScriptedHub(["坏输出1", "坏输出2", "坏输出3"])
    result = AgentOrchestrator(hub, _make_registry()).run("问题")
    assert result.success is False
    assert result.finished_reason == FINISHED_PARSE_ERROR
    assert result.parse_errors == 3


def test_unknown_tool_in_output_goes_through_retry_path() -> None:
    """未知工具名 → ReactParseError → 纠正重试（不执行、不计工具报错）。"""
    unknown = json.dumps({"thought": "x", "tool": "no_such", "arguments": {"a": 1}})
    hub = ScriptedHub([unknown, _final_output("已纠正")])
    result = AgentOrchestrator(hub, _make_registry()).run("问题")
    assert result.success is True
    assert result.tool_errors == 0
    assert result.parse_errors == 1


def test_generation_error_terminates_with_reason() -> None:
    """API 生成异常 → generation_error 终止（交由任务级重试）。"""
    hub = ScriptedHub([])
    result = AgentOrchestrator(hub, _make_registry()).run("问题")
    assert result.success is False
    assert result.finished_reason == FINISHED_GENERATION_ERROR
    assert "RuntimeError" in result.error


def test_missing_required_argument_counts_as_tool_error() -> None:
    """参数校验失败（缺必填）→ ok=False → 计入工具报错。"""
    missing = json.dumps({"thought": "x", "tool": "demo_tool", "arguments": {}})
    hub = ScriptedHub([missing, missing, missing])
    result = AgentOrchestrator(hub, _make_registry(schema_required=["item"])).run("问题")
    assert result.success is False
    assert result.finished_reason == FINISHED_TOO_MANY_TOOL_ERRORS
    assert result.tool_errors == 3
