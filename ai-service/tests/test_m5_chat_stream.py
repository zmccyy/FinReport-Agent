"""M5.04 /internal/chat/stream 与 /internal/chat/compress 单测。

覆盖：SSE 事件顺序与字段（spec §6.3.3）、ReAct 失败转 error 事件、
整流超时兜底、token 切块、摘要压缩、orchestrator on_event 回调与
conversation 透传。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from app.api.chat import get_orchestrator_factory, get_modelhub_instance
from app.core.config import Settings
from app.main import create_app
from app.modules.agent.orchestrator import AgentOrchestrator, FINISHED_MAX_STEPS
from app.modules.agent.tool_registry import ToolRegistry, ToolSpec

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class ScriptedHub:
    """脚本化 ModelHub：按序返回预设输出；耗尽后报错。"""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.generation = SimpleNamespace(text="")

    def generate(self, prompt: str, **kwargs: Any) -> SimpleNamespace:
        if not self.outputs:
            raise RuntimeError("script exhausted")
        return SimpleNamespace(text=self.outputs.pop(0))


def _tool_output(thought: str = "需要查询数据") -> str:
    return json.dumps(
        {"thought": thought, "tool": "demo_tool", "arguments": {"item": "货币资金"}},
        ensure_ascii=False,
    )


def _final_output(answer: str = "营业收入 1688 亿元") -> str:
    return json.dumps(
        {"thought": "思考完毕", "final_answer": answer}, ensure_ascii=False
    )


class FakeHub:
    """摘要压缩用假 hub（记录 prompt 并返回固定摘要）。"""

    def __init__(self, text: str = "压缩后的摘要") -> None:
        self.text = text
        self.settings = SimpleNamespace(llm_api_model="fake-model")

    def generate(self, prompt: str, **kwargs: Any) -> SimpleNamespace:
        self.last_prompt = prompt
        return SimpleNamespace(text=self.text)


def _make_registry() -> ToolRegistry:
    """注册一个回显工具。"""

    def handler(arguments: dict) -> dict:
        return {"ok": True, "data": arguments}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="demo_tool",
            description="测试工具",
            parameters={
                "type": "object",
                "properties": {"item": {"type": "string"}},
                "required": ["item"],
            },
            handler=handler,
        )
    )
    return registry


def _make_factory(hub: ScriptedHub) -> Any:
    """按 report_id 构建 orchestrator 的假工厂。"""

    def build(report_id: int) -> AgentOrchestrator:
        return AgentOrchestrator(hub, _make_registry())

    return build


def _client(hub: ScriptedHub) -> TestClient:
    app = create_app(Settings(mq_consumer_enabled=False))
    app.dependency_overrides[get_orchestrator_factory] = lambda: _make_factory(hub)
    return TestClient(app)


def _post_stream(client: TestClient, question: str = "营收多少？") -> Any:
    return client.post(
        "/internal/chat/stream",
        json={
            "sessionId": "s-1",
            "messageId": "m-1",
            "reportId": 17,
            "question": question,
            "companyContext": "贵州茅台（600519）",
        },
    )


def _parse_events(response: Any) -> list[dict[str, Any]]:
    """把 SSE 响应文本解析为事件列表。"""
    events: list[dict[str, Any]] = []
    for block in response.text.strip().split("\n\n"):
        if not block:
            continue
        event_type = "message"
        data = ""
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_type = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        events.append({"event": event_type, "data": json.loads(data)})
    return events


# ---------------------------------------------------------------------------
# SSE 流式问答
# ---------------------------------------------------------------------------


def test_stream_tool_step_then_final_answer_emits_spec_events() -> None:
    """工具调用一轮后 final_answer：事件顺序 thought→tool_call→tool_result→token→done。"""
    hub = ScriptedHub([_tool_output(), _final_output("营业收入 1688 亿元")])
    client = _client(hub)
    response = _post_stream(client)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_events(response)
    names = [e["event"] for e in events]

    assert names == ["thought", "tool_call", "tool_result", "token", "done"]
    assert events[0]["data"] == {"step": 1, "content": "需要查询数据"}
    assert events[1]["data"]["tool"] == "demo_tool"
    assert events[1]["data"]["args"] == {"item": "货币资金"}
    assert events[2]["data"]["tool"] == "demo_tool"
    assert events[2]["data"]["result"] == {"ok": True, "data": {"item": "货币资金"}}
    assert events[3]["data"]["content"] == "营业收入 1688 亿元"
    done = events[4]["data"]
    assert done["messageId"] == "m-1"
    assert done["toolsUsed"] == ["demo_tool"]
    assert done["finishedReason"] == "final_answer"
    assert done["error"] == ""


def test_stream_direct_answer_no_tool() -> None:
    """无工具直接 final_answer：仅 token + done，toolsUsed 为空。"""
    hub = ScriptedHub([_final_output("直接回答")])
    response = _post_stream(_client(hub))

    events = _parse_events(response)
    assert [e["event"] for e in events] == ["token", "done"]
    assert events[0]["data"]["content"] == "直接回答"
    assert events[1]["data"]["toolsUsed"] == []


def test_stream_long_answer_chunked_into_tokens() -> None:
    """长答案按句切块为多个 token 事件，拼接后等于原文。"""
    answer = "营业收入为 1688.65 亿元，同比增长 15.3%。" * 6
    hub = ScriptedHub([_final_output(answer)])
    response = _post_stream(_client(hub))

    tokens = [
        e["data"]["content"] for e in _parse_events(response) if e["event"] == "token"
    ]
    assert len(tokens) >= 2
    assert "".join(tokens) == answer


def test_stream_react_failure_emits_error_event() -> None:
    """ReAct 未正常结束（步数上限）→ error 事件携带原因。"""
    hub = ScriptedHub([_tool_output()] * 8)
    response = _post_stream(_client(hub))

    events = _parse_events(response)
    assert events[-1]["event"] == "error"
    assert events[-1]["data"]["code"] == "CHAT_FAILED"
    assert events[-1]["data"]["finishedReason"] == FINISHED_MAX_STEPS
    # error 前仍把可见的 thought/tool_call/tool_result 事件流出了
    assert any(e["event"] == "thought" for e in events)


def test_stream_generation_error_emits_error_event() -> None:
    """LLM 生成阶段异常（脚本耗尽）→ error 事件，不挂断连接。"""
    hub = ScriptedHub([])
    response = _post_stream(_client(hub))

    events = _parse_events(response)
    assert events[-1]["event"] == "error"
    assert events[-1]["data"]["code"] == "CHAT_FAILED"
    assert "RuntimeError" in events[-1]["data"]["message"]


def test_stream_rejects_missing_report_id() -> None:
    """缺少 reportId → 422（FastAPI 校验）。"""
    app = create_app(Settings(mq_consumer_enabled=False))
    app.dependency_overrides[get_orchestrator_factory] = lambda: _make_factory(
        ScriptedHub([])
    )
    client = TestClient(app)
    response = client.post(
        "/internal/chat/stream",
        json={"sessionId": "s-1", "messageId": "m-1", "question": "营收？"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 会话摘要压缩（M5.06 前置）
# ---------------------------------------------------------------------------


def test_compress_returns_summary() -> None:
    """/internal/chat/compress 返回摘要与模型名。"""
    hub = FakeHub("茅台 2025 年营收 1688 亿")
    app = create_app(Settings(mq_consumer_enabled=False))
    app.dependency_overrides[get_modelhub_instance] = lambda: hub
    client = TestClient(app)

    response = client.post(
        "/internal/chat/compress",
        json={
            "sessionId": "s-1",
            "messages": [{"role": "user", "content": "营收多少？"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["summary"] == "茅台 2025 年营收 1688 亿"
    assert response.json()["model"] == "fake-model"
    assert "压缩" in hub.last_prompt


# ---------------------------------------------------------------------------
# orchestrator on_event / conversation（单元层面）
# ---------------------------------------------------------------------------


def test_orchestrator_on_event_emits_step_events() -> None:
    """on_event 回调按 thought→tool_call→tool_result 顺序收到事件。"""
    hub = ScriptedHub([_tool_output("先查数据"), _final_output("答案")])
    orchestrator = AgentOrchestrator(hub, _make_registry())
    received: list[dict[str, Any]] = []

    result = orchestrator.run("问题", on_event=received.append)

    assert result.success is True
    assert [e["type"] for e in received] == ["thought", "tool_call", "tool_result"]
    assert received[0]["content"] == "先查数据"
    assert received[1]["tool"] == "demo_tool"
    assert received[2]["result"] == {"ok": True, "data": {"item": "货币资金"}}


def test_orchestrator_conversation_passed_to_prompt() -> None:
    """conversation 历史注入 user prompt（多轮追问背景）。"""
    prompts: list[str] = []

    class RecordingHub:
        def generate(self, prompt: str, **kwargs: Any) -> SimpleNamespace:
            prompts.append(prompt)
            return SimpleNamespace(text=_final_output("答案"))

    orchestrator = AgentOrchestrator(RecordingHub(), _make_registry())
    orchestrator.run(
        "那毛利率呢？",
        conversation=[
            {"role": "user", "content": "营收多少？"},
            {"role": "assistant", "content": "1688 亿"},
        ],
    )

    assert prompts, "generate 应至少被调用一次"
    first_prompt = prompts[0]
    assert "对话历史" in first_prompt
    assert "用户: 营收多少？" in first_prompt
    assert "助手: 1688 亿" in first_prompt
    assert "那毛利率呢？" in first_prompt
