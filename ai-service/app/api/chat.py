"""M5.04 内部问答端点（spec §6.4 /internal/chat/stream + /internal/chat/compress）。

问答链路的数据面走 HTTP SSE（spec §3.3）：L2 以 WebClient 拉流本端点，
逐事件透传前端。ReAct 循环在后台线程执行（DeepSeek 调用是同步阻塞的），
每完成一步即经 ``asyncio.Queue`` 把 thought / tool_call / tool_result 事件
推回 SSE 流——首个 thought 事件远早于最终答案，满足「首 token < 15s」。
最终答案按句切块为 token 事件；末尾以 done（含 messageId / 用量 / 工具
清单）收尾，失败以 error 事件终结（可含 LLM 端错误信息）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.config import Settings
from app.core.mysql_client import ReadOnlyMySqlClient
from app.modules.agent.orchestrator import (
    AgentOrchestrator,
    AgentResult,
)
from app.modules.agent.tools import build_default_registry
from app.modules.modelhub.modelhub import ModelHub, get_modelhub
from app.schemas.chat import (
    ChatCompressRequest,
    ChatCompressResponse,
    ChatStreamRequest,
    build_chat_event,
)
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)
router = APIRouter(tags=["chat"])

# 单次问答整流超时。spec §3.7 单轮「首 token <15s、完整 <30s、超时 60s」
# 以单次生成为口径；多步 ReAct（步数上限 8，单步生成 SLA 300s）实测单轮
# 可达 100s+（真实冒烟 6-7 次工具调用 127s），60s 整流会切断正常链路。
# 120s 是多步 ReAct 的整流上限（防无限挂起），完整回答 SLA 在 M5.09 实测。
CHAT_STREAM_TIMEOUT_SECONDS = 120.0
# 最终答案 token 事件切块目标长度（字符）。
TOKEN_CHUNK_SIZE = 64
# 摘要压缩输出上限（M5.06）。
COMPRESS_MAX_TOKENS = 512


def get_orchestrator_factory() -> Callable[[int], AgentOrchestrator]:
    """生产装配：返回按 report_id 构建 orchestrator 的工厂。

    工具注册表按对话绑定的报表构建（query_statement 等只读该报告数据），
    因此 orchestrator 无法在应用启动时单例化，只能每请求构建。测试经
    ``app.dependency_overrides`` 替换本工厂。

    Returns:
        ``report_id -> AgentOrchestrator`` 工厂（registry 绑定该报表）。
    """
    settings = Settings()
    hub = get_modelhub()
    reader = ReadOnlyMySqlClient(settings)

    def build(report_id: int) -> AgentOrchestrator:
        registry = build_default_registry(
            reader,
            report_id,
            embedder=hub.embed,
            milvus_host=settings.milvus_host,
            milvus_port=settings.milvus_port,
        )
        return AgentOrchestrator(hub, registry)

    return build


def get_modelhub_instance() -> ModelHub:
    """Provide the process-wide ModelHub singleton (test override point)."""
    return get_modelhub()


@router.post("/internal/chat/stream")
async def stream_chat(
    request: ChatStreamRequest,
    factory: Annotated[
        Callable[[int], AgentOrchestrator], Depends(get_orchestrator_factory)
    ],
) -> StreamingResponse:
    """流式执行一轮 ReAct 问答，事件经 SSE 推送（spec §6.3.3 事件格式）。

    Args:
        request: 会话/问题/上下文请求体。
        factory: orchestrator 工厂（按 report_id 构建工具注册表）。

    Returns:
        ``text/event-stream``：thought / tool_call / tool_result / token /
        done 或 error 事件流。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    orchestrator = factory(request.report_id)
    conversation = [
        {"role": turn.role, "content": turn.content} for turn in request.history
    ]

    def emit(event: dict[str, Any]) -> None:
        # 工作线程同步回调 → 事件循环队列（call_soon_threadsafe 保证线程安全）。
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def run_blocking() -> AgentResult:
        try:
            return orchestrator.run(
                request.question,
                company_context=request.company_context or "",
                conversation=conversation,
                summary=request.summary or "",
                on_event=emit,
            )
        finally:
            # 线程结束哨兵：生成器据此退出队列循环（无论成败都必须投递，
            # 否则 queue.get() 永久阻塞）。
            loop.call_soon_threadsafe(queue.put_nowait, None)

    async def event_stream() -> AsyncIterator[str]:
        """消费事件队列并渲染 SSE；整流超时兜底。"""
        task = asyncio.create_task(asyncio.to_thread(run_blocking))
        token_count = 0
        tools_used: list[str] = []
        try:
            while True:
                event = await asyncio.wait_for(
                    queue.get(), timeout=CHAT_STREAM_TIMEOUT_SECONDS
                )
                if event is None:
                    break
                yield _render_step_event(event, tools_used)
            result = await task
            if result.success:
                for chunk in _chunk_answer(result.answer or ""):
                    token_count += 1
                    yield build_chat_event("token", {"content": chunk})
                yield build_chat_event(
                    "done",
                    {
                        "messageId": request.message_id,
                        "tokenCount": token_count,
                        "toolsUsed": tools_used,
                        "finishedReason": result.finished_reason,
                        "error": "",
                    },
                )
            else:
                LOGGER.warning(
                    "[chat/stream] ReAct 未正常结束 sessionId=%s reason=%s error=%s",
                    request.session_id,
                    result.finished_reason,
                    result.error,
                )
                yield build_chat_event(
                    "error",
                    {
                        "code": "CHAT_FAILED",
                        "message": result.error
                        or f"ReAct 终止：{result.finished_reason}",
                        "finishedReason": result.finished_reason,
                    },
                )
        except TimeoutError:
            task.cancel()
            LOGGER.error("[chat/stream] 整流超时 sessionId=%s", request.session_id)
            yield build_chat_event(
                "error",
                {"code": "CHAT_TIMEOUT", "message": "问答超时，请稍后重试"},
            )
        except Exception as error:  # 防御：流内异常转 error 事件而非断连
            LOGGER.exception(
                "[chat/stream] 流生成异常 sessionId=%s", request.session_id
            )
            yield build_chat_event(
                "error",
                {"code": "CHAT_FAILED", "message": f"{type(error).__name__}: {error}"},
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/internal/chat/compress", response_model=ChatCompressResponse)
async def compress_chat(
    request: ChatCompressRequest,
    hub: Annotated[ModelHub, Depends(get_modelhub_instance)],
) -> ChatCompressResponse:
    """把会话历史压缩为一段关键事实摘要（M5.06 第 11 轮触发）。

    Args:
        request: 会话消息列表。
        hub: 注入的 ModelHub（测试可替换）。

    Returns:
        摘要文本；压缩失败抛 AiException（由 L2 降级为保留原始消息）。
    """
    prompt = _build_compress_prompt(request.messages)
    generation = await asyncio.to_thread(
        hub.generate,
        prompt,
        temperature=0.0,
        max_new_tokens=COMPRESS_MAX_TOKENS,
    )
    summary = generation.text.strip()
    return ChatCompressResponse(summary=summary, model=hub.settings.llm_api_model)


def _build_compress_prompt(messages: list[Any]) -> str:
    """渲染摘要压缩 prompt（保留关键事实：公司/科目/数值/结论）。"""
    transcript = "\n".join(
        f"{'用户' if turn.role == 'user' else '助手'}: {turn.content}"
        for turn in messages
    )
    return (
        "请把以下 A 股财报问答对话压缩为一段不超过 200 字的摘要，"
        "必须保留关键事实：公司名称、科目、数值、结论；"
        "不要补充对话中不存在的信息。\n\n"
        f"{transcript}"
    )


def _render_step_event(event: dict[str, Any], tools_used: list[str]) -> str:
    """把 orchestrator 回调事件渲染为 SSE 事件（spec §6.3.3 字段名）。"""
    event_type = event["type"]
    if event_type == "thought":
        return build_chat_event(
            "thought", {"step": event["step"], "content": event["content"]}
        )
    if event_type == "tool_call":
        tools_used.append(event["tool"])
        return build_chat_event(
            "tool_call",
            {"step": event["step"], "tool": event["tool"], "args": event["args"]},
        )
    if event_type == "tool_result":
        return build_chat_event(
            "tool_result",
            {"step": event["step"], "tool": event["tool"], "result": event["result"]},
        )
    LOGGER.warning("[chat/stream] 未知事件类型 %s", event_type)
    return build_chat_event("token", {"content": ""})


def _chunk_answer(text: str, size: int = TOKEN_CHUNK_SIZE) -> list[str]:
    """把最终答案切为 SSE token 事件块（句末/段落优先，不割裂中文词语）。

    Args:
        text: 最终答案全文。
        size: 目标块长度（字符）。

    Returns:
        顺序文本块列表；空文本返回空列表。
    """
    if not text:
        return []
    # 先按段落/句号/分号分句，再贪心合并到目标长度。
    import re

    sentences = re.split(r"(?<=[。！？；\n])", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(current) + len(sentence) <= size:
            current += sentence
            continue
        if current:
            chunks.append(current)
        # 超长单句按固定长度硬切，避免无限循环。
        while len(sentence) > size:
            chunks.append(sentence[:size])
            sentence = sentence[size:]
        current = sentence
    if current:
        chunks.append(current)
    return chunks
