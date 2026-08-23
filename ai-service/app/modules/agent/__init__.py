"""M5.01-M5.03 Agent 问答模块（spec §2.3 M9）。

ReAct 循环 + 6 工具注册表 + json_mode 强约束。Phase 1 交付：
``AgentOrchestrator`` 与 ``ToolRegistry`` 均为依赖注入、可单测的纯组件，
SSE 端点（M5.04）与 MQ chat 消费（M5.05）在后续里程碑接入。
"""

from app.modules.agent.orchestrator import AgentOrchestrator, AgentResult
from app.modules.agent.react_parser import FinalAnswer, ToolCall
from app.modules.agent.tool_registry import ToolRegistry, ToolSpec
from app.modules.agent.tools import build_default_registry

__all__ = [
    "AgentOrchestrator",
    "AgentResult",
    "ToolCall",
    "FinalAnswer",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
]
