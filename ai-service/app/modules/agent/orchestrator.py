"""M5.01 AgentOrchestrator ReAct 循环（spec §2.3 M9）。

Thought → Action → Observation → Thought… → Final Answer 循环：

1. system prompt 注入工具清单（JSON Schema）；user 消息携带问题与
   逐步历史（前缀式拼接，ModelHub.generate 的 prompt + system_prompt）。
2. 每步 ``generate(json_mode=True)`` → ``react_parser`` 解析输出；
   解析失败在循环内追加纠正提示重试（连续 3 次终止）。
3. 工具调用经 ``ToolRegistry``（存在性 + 必填参数检查 + 异常捕获），
   执行结果字符串化为 Observation（截断防 prompt 膨胀）。
4. 终止条件：final_answer / 步数上限 8 / 工具报错 3 次 / 解析失败 3 次
   / 生成阶段 API 异常（由调用方按任务级重试处理）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from app.modules.agent.prompts import (
    MAX_OBSERVATION_CHARS,
    build_react_prompt,
    build_retry_prompt,
    build_system_prompt,
)
from app.modules.agent.react_parser import (
    MAX_PARSE_ERRORS,
    FinalAnswer,
    ReactParseError,
    ToolCall,
    parse_llm_output,
)
from app.modules.agent.tool_registry import ToolRegistry
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# ReAct 终止原因枚举（AgentResult.finished_reason 取值）。
FINISHED_FINAL = "final_answer"
FINISHED_MAX_STEPS = "max_steps"
FINISHED_TOO_MANY_TOOL_ERRORS = "too_many_tool_errors"
FINISHED_PARSE_ERROR = "parse_error"
FINISHED_GENERATION_ERROR = "generation_error"

# 兜底合成指令（M6.08 评估发现 5：平安 q2 8 次工具调用后放弃，answer 为空）。
# 步数/错误熔断后追加一轮「仅凭已有 Observation 直接作答」的合成调用，
# 保证用户永远拿到非空回答；Observation 不足以回答时要求诚实说明而非编造。
_FALLBACK_DIRECTIVE = (
    "\n你在此前的 ReAct 过程中已耗尽工具调用机会，未能输出最终回答。"
    "现在请只依据上面「已执行的步骤」中各 Observation 里的真实数据，"
    "用一段不超过 200 字的中文正文直接回答最初的用户问题：\n"
    "1. 只引用 Observation 中出现的数据与结论，禁止编造任何数值；\n"
    "2. 若 Observation 不足以回答，请明确说明「根据当前可获取的数据暂时无法"
    "回答该问题」并简述原因（如科目未找到、工具不可用）；\n"
    "3. 直接输出回答正文，不要输出 JSON、thought、工具调用或任何解释性元话语。"
)
# 合成调用也失败时的最终静态兜底（保证非空回答，含终止原因供排查）。
_FALLBACK_STATIC = (
    "抱歉，本次问答未能生成有效回答（{reason}）。请稍后重试，"
    "或换一种问法（如指定报表类型与期间）。"
)


@dataclass(frozen=True)
class AgentStep:
    """单步记录（transcript 用）。"""

    thought: str
    action: str
    observation: str


@dataclass(frozen=True)
class AgentResult:
    """ReAct 运行结果。"""

    answer: str | None = None
    finished_reason: str = FINISHED_FINAL
    steps: list[AgentStep] = field(default_factory=list)
    tool_errors: int = 0
    parse_errors: int = 0
    total_steps: int = 0
    error: str = ""

    @property
    def success(self) -> bool:
        """以 final_answer 正常结束。"""
        return self.finished_reason == FINISHED_FINAL


class AgentOrchestrator:
    """ReAct 循环执行器（工具注册表 + ModelHub 依赖注入）。"""

    def __init__(
        self,
        hub: Any,
        registry: ToolRegistry,
        *,
        max_steps: int = 8,
        max_tool_errors: int = 3,
        temperature: float = 0.3,
        max_new_tokens: int = 4096,
        timeout_seconds: float | None = None,
        unit_hint: str = "",
    ) -> None:
        """Configure the orchestrator.

        Args:
            hub: ``ModelHub``（generate 支持 json_mode）。
            registry: 已注册工具的 ``ToolRegistry``。
            max_steps: 步数上限（spec M9：8）。
            max_tool_errors: 工具报错上限（spec M9：3）。
            temperature: ReAct 采样温度（多步探索需要少量随机性）。
            max_new_tokens: 单步输出上限（每步仅一个 JSON，4096 足够）。
            timeout_seconds: 单步生成超时（None 用后端默认）。
            unit_hint: 报表数值单位（如「百万元」）；非空时 system prompt
                注入金额单位铁律（M6.08 评估发现 3）。
        """
        self.hub = hub
        self.registry = registry
        self.max_steps = max_steps
        self.max_tool_errors = max_tool_errors
        self.temperature = temperature
        self.max_new_tokens = max_new_tokens
        self.timeout_seconds = timeout_seconds
        self._system_prompt = build_system_prompt(registry.specs(), unit_hint=unit_hint)

    def run(
        self,
        question: str,
        *,
        company_context: str = "",
        conversation: list[dict[str, str]] | None = None,
        summary: str = "",
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentResult:
        """执行一轮 ReAct 对话。

        Args:
            question: 用户问题。
            company_context: 报表上下文（如「贵州茅台（600519），报告期
                2025-12-31」）；帮助模型正确选用工具。
            conversation: 此前对话轮次（每项含 ``role`` / ``content``），
                供多轮追问时携带上下文；None 表示首轮。
            summary: 超长会话压缩摘要（M5.06）；为空表示无摘要。
            on_event: 可选事件回调（M5.04 SSE 流式输出用）。每完成一步
                依次收到 ``thought`` / ``tool_call`` / ``tool_result``
                事件（dict 含 ``type`` 等字段），在 ReAct 循环内同步调用。

        Returns:
            ``AgentResult``；``success`` 为 False 时 ``finished_reason``
            说明终止原因，``error`` 携带生成阶段异常描述。
        """
        steps_done = 0
        parse_errors = 0
        tool_errors = 0
        transcript: list[AgentStep] = []
        prompt_extra = ""
        known_tools = self.registry.known_tools()

        def finish(
            finished_reason: str,
            *,
            total_steps: int,
            error: str,
        ) -> AgentResult:
            """非 final_answer 终止的统一出口：先做兜底合成再返回。

            兜底合成（M6.08 评估发现 5）：把已收集的 Observation 交给 LLM
            直接作答，失败则回退静态提示文案；任何情况下 answer 非空，
            ``finished_reason`` 保留真实终止原因供前端/评估观测。
            """
            return self._synthesize_fallback(
                question,
                history=[
                    {
                        "thought": step.thought,
                        "action": step.action,
                        "observation": step.observation,
                    }
                    for step in transcript
                ],
                steps=transcript,
                company_context=company_context,
                conversation=conversation,
                summary=summary,
                prompt_extra=prompt_extra,
                finished_reason=finished_reason,
                tool_errors=tool_errors,
                parse_errors=parse_errors,
                total_steps=total_steps,
                error=error,
            )

        while steps_done < self.max_steps:
            history = [
                {
                    "thought": step.thought,
                    "action": step.action,
                    "observation": step.observation,
                }
                for step in transcript
            ]
            prompt = (
                build_react_prompt(
                    question,
                    history,
                    company_context=company_context,
                    conversation=conversation,
                    summary=summary,
                )
                + prompt_extra
            )
            try:
                generation = self.hub.generate(
                    prompt,
                    system_prompt=self._system_prompt,
                    json_mode=True,
                    temperature=self.temperature,
                    max_new_tokens=self.max_new_tokens,
                    timeout_seconds=self.timeout_seconds,
                )
            except Exception as error:
                LOGGER.exception("[AgentOrchestrator] 生成失败 step=%d", steps_done)
                return finish(
                    FINISHED_GENERATION_ERROR,
                    total_steps=steps_done,
                    error=f"{type(error).__name__}: {error}",
                )
            steps_done += 1

            try:
                parsed = parse_llm_output(generation.text, known_tools)
            except ReactParseError as error:
                parse_errors += 1
                if parse_errors >= MAX_PARSE_ERRORS:
                    return finish(
                        FINISHED_PARSE_ERROR,
                        total_steps=steps_done,
                        error=f"连续 {parse_errors} 次输出解析失败: {error}",
                    )
                prompt_extra += build_retry_prompt(str(error))
                continue

            if isinstance(parsed, FinalAnswer):
                return AgentResult(
                    answer=parsed.answer,
                    finished_reason=FINISHED_FINAL,
                    steps=transcript,
                    tool_errors=tool_errors,
                    parse_errors=parse_errors,
                    total_steps=steps_done,
                )

            step_index = len(transcript) + 1
            if on_event is not None:
                on_event(
                    {
                        "type": "thought",
                        "step": step_index,
                        "content": parsed.thought,
                    }
                )
                on_event(
                    {
                        "type": "tool_call",
                        "step": step_index,
                        "tool": parsed.tool,
                        "args": parsed.arguments,
                    }
                )
            result = self._execute_tool(parsed)
            observation = _format_observation(result)
            if on_event is not None:
                on_event(
                    {
                        "type": "tool_result",
                        "step": step_index,
                        "tool": parsed.tool,
                        "result": result,
                    }
                )
            transcript.append(
                AgentStep(
                    thought=parsed.thought,
                    action=_format_action(parsed),
                    observation=observation,
                )
            )
            if not result.get("ok"):
                tool_errors += 1
                if tool_errors >= self.max_tool_errors:
                    return finish(
                        FINISHED_TOO_MANY_TOOL_ERRORS,
                        total_steps=steps_done,
                        error=f"连续 {tool_errors} 次工具报错",
                    )

        return finish(
            FINISHED_MAX_STEPS,
            total_steps=steps_done,
            error=f"达到步数上限 {self.max_steps}",
        )

    def _synthesize_fallback(
        self,
        question: str,
        *,
        history: list[dict[str, str]],
        steps: list[AgentStep],
        company_context: str,
        conversation: list[dict[str, str]] | None,
        summary: str,
        prompt_extra: str,
        finished_reason: str,
        tool_errors: int,
        parse_errors: int,
        total_steps: int,
        error: str,
    ) -> AgentResult:
        """熔断/异常终止后的兜底回答（M6.08 评估发现 5）。

        追加一轮「仅凭已有 Observation 直接作答」的合成调用（关闭 json_mode，
        输出纯文本正文）；合成调用自身失败（hub 异常/空输出）时回退静态
        提示文案。任何分支都保证 ``answer`` 非空。

        Args:
            question: 用户问题（与 run 入参一致）。
            history: 已执行步骤（thought/action/observation，prompt 组装用）。
            steps: 已执行步骤对象（结果透传，transcript 展示用）。
            company_context / conversation / summary / prompt_extra: 与主循环
                相同的 prompt 组装参数。
            finished_reason: 真实终止原因（保留在结果中，不因兜底改写）。
            tool_errors / parse_errors / total_steps / error: 计数与异常描述。

        Returns:
            ``AgentResult``（answer 非空，finished_reason 为真实终止原因）。
        """
        prompt = (
            build_react_prompt(
                question,
                history,
                company_context=company_context,
                conversation=conversation,
                summary=summary,
            )
            + prompt_extra
            + _FALLBACK_DIRECTIVE
        )
        static = _FALLBACK_STATIC.format(
            reason=error or f"ReAct 终止：{finished_reason}"
        )
        try:
            generation = self.hub.generate(
                prompt,
                system_prompt=self._system_prompt,
                json_mode=False,
                temperature=self.temperature,
                max_new_tokens=self.max_new_tokens,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as fallback_error:  # noqa: BLE001 — 兜底路径吞异常
            LOGGER.warning(
                "[AgentOrchestrator] 兜底合成调用失败 reason=%s error=%s",
                finished_reason,
                fallback_error,
            )
            return AgentResult(
                answer=static,
                finished_reason=finished_reason,
                steps=steps,
                tool_errors=tool_errors,
                parse_errors=parse_errors,
                total_steps=total_steps,
                error=error,
            )
        answer = (generation.text or "").strip()
        if not answer:
            LOGGER.warning(
                "[AgentOrchestrator] 兜底合成为空输出 reason=%s", finished_reason
            )
            answer = static
        LOGGER.info(
            "[AgentOrchestrator] 兜底回答已生成 reason=%s chars=%d",
            finished_reason,
            len(answer),
        )
        return AgentResult(
            answer=answer,
            finished_reason=finished_reason,
            steps=steps,
            tool_errors=tool_errors,
            parse_errors=parse_errors,
            total_steps=total_steps,
            error=error,
        )

    def _execute_tool(self, parsed: ToolCall) -> dict[str, Any]:
        """执行一次工具调用（参数校验 + 注册表执行，异常转 ok=False）。"""
        validation = self.registry.validate_arguments(parsed.tool, parsed.arguments)
        if validation is not None:
            return {"ok": False, "error": validation}
        return self.registry.execute(parsed.tool, parsed.arguments)


def _format_action(parsed: ToolCall) -> str:
    """工具调用文本化（transcript 展示用）。"""
    arguments = json.dumps(parsed.arguments, ensure_ascii=False)
    return f"{parsed.tool}({arguments})"


def _format_observation(result: dict[str, Any]) -> str:
    """工具结果字符串化（Observation），超长截断防 prompt 膨胀。"""
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > MAX_OBSERVATION_CHARS:
        text = text[:MAX_OBSERVATION_CHARS] + "…(截断)"
    return text


__all__ = [
    "AgentOrchestrator",
    "AgentResult",
    "AgentStep",
    "FINISHED_FINAL",
    "FINISHED_MAX_STEPS",
    "FINISHED_TOO_MANY_TOOL_ERRORS",
    "FINISHED_PARSE_ERROR",
    "FINISHED_GENERATION_ERROR",
]
