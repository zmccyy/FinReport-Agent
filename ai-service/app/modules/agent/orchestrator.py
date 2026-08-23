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
from typing import Any

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
        """
        self.hub = hub
        self.registry = registry
        self.max_steps = max_steps
        self.max_tool_errors = max_tool_errors
        self.temperature = temperature
        self.max_new_tokens = max_new_tokens
        self.timeout_seconds = timeout_seconds
        self._system_prompt = build_system_prompt(registry.specs())

    def run(
        self,
        question: str,
        *,
        company_context: str = "",
    ) -> AgentResult:
        """执行一轮 ReAct 对话。

        Args:
            question: 用户问题。
            company_context: 报表上下文（如「贵州茅台（600519），报告期
                2025-12-31」）；帮助模型正确选用工具。

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
                build_react_prompt(question, history, company_context=company_context)
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
                return AgentResult(
                    finished_reason=FINISHED_GENERATION_ERROR,
                    steps=transcript,
                    tool_errors=tool_errors,
                    parse_errors=parse_errors,
                    total_steps=steps_done,
                    error=f"{type(error).__name__}: {error}",
                )
            steps_done += 1

            try:
                parsed = parse_llm_output(generation.text, known_tools)
            except ReactParseError as error:
                parse_errors += 1
                if parse_errors >= MAX_PARSE_ERRORS:
                    return AgentResult(
                        finished_reason=FINISHED_PARSE_ERROR,
                        steps=transcript,
                        tool_errors=tool_errors,
                        parse_errors=parse_errors,
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

            result = self._execute_tool(parsed)
            observation = _format_observation(result)
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
                    return AgentResult(
                        finished_reason=FINISHED_TOO_MANY_TOOL_ERRORS,
                        steps=transcript,
                        tool_errors=tool_errors,
                        parse_errors=parse_errors,
                        total_steps=steps_done,
                        error=f"连续 {tool_errors} 次工具报错",
                    )

        return AgentResult(
            finished_reason=FINISHED_MAX_STEPS,
            steps=transcript,
            tool_errors=tool_errors,
            parse_errors=parse_errors,
            total_steps=steps_done,
            error=f"达到步数上限 {self.max_steps}",
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
