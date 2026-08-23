"""M5.04 问答请求/响应 schema（spec §6.3.3 / §6.4 /internal/chat/stream）。"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatTurn(BaseModel):
    """一轮历史问答（L2 M5.06 会话上下文透传）。"""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class ChatStreamRequest(BaseModel):
    """L2 → L3 流式问答请求体。"""

    session_id: str = Field(alias="sessionId", min_length=1, max_length=64)
    message_id: str = Field(alias="messageId", min_length=1, max_length=64)
    report_id: int = Field(alias="reportId", gt=0)
    question: str = Field(min_length=1, max_length=4000)
    company_context: str | None = Field(
        default=None, alias="companyContext", max_length=200
    )
    history: list[ChatTurn] = Field(default_factory=list, max_length=30)
    summary: str | None = Field(default=None, max_length=1000)

    model_config = {"populate_by_name": True}


class ChatCompressRequest(BaseModel):
    """L2 → L3 会话摘要压缩请求体（M5.06 第 11 轮触发）。"""

    session_id: str = Field(alias="sessionId", min_length=1, max_length=64)
    messages: list[ChatTurn] = Field(min_length=1, max_length=30)

    model_config = {"populate_by_name": True}


class ChatCompressResponse(BaseModel):
    """会话摘要压缩结果。"""

    summary: str = Field(min_length=1)
    model: str | None = None


def build_chat_event(event_type: str, data: dict[str, Any]) -> str:
    """渲染一条 SSE 事件（event + data 两行，UTF-8）。

    Args:
        event_type: SSE event 名（thought / tool_call / tool_result / token / done）。
        data: JSON 可序列化的事件数据。

    Returns:
        符合 text/event-stream 协议的完整事件文本（以空行结尾）。
    """
    import json

    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"
