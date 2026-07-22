"""M3.02 LLM 复核勾稽（spec §2.3 M8 + plan M3.02）。

职责：

* 硬编码规则失败（``severity ∈ {WARN, ERROR}``）时调用 7B 复核差异原因。
* 解析 LLM 输出的 JSON，回填 ``RuleResult.note`` 并置 ``llm_reviewed=True``。
* 失败降级（spec §10.3）：超时 / JSON 解析失败 / 模型异常 都不抛，保留原
  ``note`` 并追加降级标记，保证 Reasoner 链路不被 LLM 失败阻断。

设计要点：

1. **触发条件** — 仅复核 ``WARN`` / ``ERROR`` 规则。``INFO`` 已通过无需复核；
   ``CRITICAL`` 是科目缺失或规则异常，LLM 无数据可分析。
2. **与 RuleEngine 解耦** — ``RuleEngine`` 保持纯同步、无 IO；``LLMReviewer``
   是独立的异步类，调用方（M3.04 L2 编排）按需 ``await reviewer.review(...)``。
3. **同步 generate 包到 to_thread** — ``ModelHub.generate`` 是 torch 阻塞
   调用；用 ``asyncio.to_thread`` 避免阻塞事件循环，同时不引入并发（spec §8.1
   限制单进程只能装 1 个 7B，并发调用反而争抢）。
4. **不可变模型** — ``RuleResult`` / ``CheckResult`` 是 Pydantic 不可变模型；
   复核产出新对象（``model_copy(update=...)``），原对象保持不变便于对比。
5. **prompt 强约束 JSON** — 对齐 ``extractor.prompts`` 风格，输出
   ``{"reason": "...", "is_explained": true/false}``，``is_explained=false``
   表示 LLM 也无法解释（可能是科目分类错误或数据问题）。
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from typing import Any

from app.modules.modelhub.modelhub import ModelHub
from app.schemas.reasoning import (
    CheckResult,
    RuleResult,
    Severity,
    StatementSnapshot,
)
from app.schemas.statement import StatementType
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# 仅复核 WARN / ERROR；INFO 已通过、CRITICAL 缺数据无意义。
_REVIEWABLE_SEVERITIES: frozenset[Severity] = frozenset({Severity.WARN, Severity.ERROR})

# Strip ```json ... ``` fenced code blocks (Qwen sometimes wraps output).
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
# Capture the first balanced-looking object substring as last-resort fallback.
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# 复核 prompt 默认参数（spec §3.7 SLA：REASON 链路 60s 超时）。
_DEFAULT_MAX_NEW_TOKENS = 512
_DEFAULT_TEMPERATURE = 0.1  # 复核需稳定推理，低温但允许少量采样避免死循环
_DEFAULT_TIMEOUT_SECONDS = 60.0


# ============================================================================
# Prompt 构建
# ============================================================================


_SYSTEM_PROMPT = (
    "你是一名专业的A股财报审计专家。任务是分析三表勾稽差异原因，"
    "严格输出 JSON 对象，禁止输出任何额外文字、Markdown 代码块或解释。"
    "reason 字段用中文说明差异的潜在原因（如科目分类差异、披露口径不同、"
    "数据录入错误等）；is_explained 字段表示你是否能合理解释该差异。"
)

_OUTPUT_SCHEMA_HINT = """{
  "reason": "差异原因说明（中文，不超过 200 字）",
  "is_explained": true
}"""

# 三表中文标签（与 StatementType.chinese_name 对齐，但避免循环 import）。
_STATEMENT_LABELS: dict[StatementType, str] = {
    StatementType.BALANCE_SHEET: "资产负债表",
    StatementType.INCOME_STATEMENT: "利润表",
    StatementType.CASH_FLOW: "现金流量表",
}


def build_review_prompt(
    rule: RuleResult,
    snapshot: StatementSnapshot,
) -> str:
    """构建单条规则的 LLM 复核 prompt（chat-style，对齐 extractor 风格）。

    Args:
        rule: 失败的规则结果（含 actual / expected / diff / note）。
        snapshot: 三表快照，用于回填相关科目上下文。

    Returns:
        单个 prompt 字符串，ready for ``ModelHub.generate``。
    """
    # 收集三表前若干科目作为上下文（避免 prompt 过长撑爆 7B 上下文）。
    context_lines: list[str] = []
    for st_type, items in snapshot.statements.items():
        label = _STATEMENT_LABELS.get(st_type, st_type.value)
        if not items:
            continue
        # 取前 15 个科目，避免 prompt 过长。
        sample = list(items.items())[:15]
        formatted = ", ".join(f"{name}={value}" for name, value in sample)
        context_lines.append(f"  {label}: {formatted}")
    context_block = "\n".join(context_lines) if context_lines else "  (无科目数据)"

    # 数值字段格式化（Decimal → str，避免科学计数法）。
    def _fmt(v: Decimal | None) -> str:
        return "N/A" if v is None else f"{v:.2f}"

    header_lines = [
        f"规则：{rule.rule_name}（{rule.rule_type.value}）",
        f"严重度：{rule.severity.value}",
        f"期望值（等式右侧）：{_fmt(rule.expected)}",
        f"实际值（等式左侧）：{_fmt(rule.actual)}",
        f"差异（actual - expected）：{_fmt(rule.diff)}",
        f"容差：{_fmt(rule.tolerance)}",
    ]
    if rule.note:
        header_lines.append(f"硬编码规则提示：{rule.note}")
    if rule.missing_items:
        header_lines.append(f"缺失科目：{', '.join(rule.missing_items)}")

    header = "\n".join(header_lines)
    schema_block = f"输出格式（严格 JSON）：\n{_OUTPUT_SCHEMA_HINT}"

    return (
        f"<|im_start|>system\n{_SYSTEM_PROMPT}<|im_end|>\n"
        f"<|im_start|>user\n{header}\n\n"
        f"三表科目上下文：\n{context_block}\n\n"
        f"{schema_block}\n"
        f"<|im_end|>\n<|im_start|>assistant\n"
    )


# ============================================================================
# LLMReviewer
# ============================================================================


class LLMReviewer:
    """LLM 复核勾稽 — 对失败的硬编码规则调用 7B 解释差异原因。

    Attributes:
        hub: ModelHub 实例（必须已加载 7B；调用方负责 ``load_for_scene`` +
            ``model_lock``，与 ``Extractor`` 保持一致）。
        max_new_tokens: 复核输出最大 token 数。
        temperature: 采样温度（0 = greedy）。
        timeout_seconds: 单次复核 SLA 超时。
    """

    def __init__(
        self,
        hub: ModelHub,
        *,
        max_new_tokens: int | None = None,
        temperature: float = _DEFAULT_TEMPERATURE,
        timeout_seconds: float | None = None,
    ) -> None:
        """初始化复核器。

        Args:
            hub: ModelHub 实例。
            max_new_tokens: 覆盖默认 max tokens。
            temperature: 覆盖默认温度。
            timeout_seconds: 覆盖默认 SLA 超时。
        """
        self.hub = hub
        self.settings = hub.settings
        self.max_new_tokens = (
            max_new_tokens if max_new_tokens is not None else _DEFAULT_MAX_NEW_TOKENS
        )
        self.temperature = temperature
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else _DEFAULT_TIMEOUT_SECONDS
        )

    async def review(
        self,
        check_result: CheckResult,
        snapshot: StatementSnapshot,
    ) -> CheckResult:
        """复核 ``CheckResult`` 中所有 WARN/ERROR 规则，回填 note。

        顺序处理（spec §8.1 单进程只能装 1 个 7B，并发无收益）；每条规则
        独立降级，单条失败不影响其他规则。

        Args:
            check_result: ``RuleEngine.check`` 产出。
            snapshot: 三表快照（与 ``check_result`` 同源）。

        Returns:
            新的 ``CheckResult``；复核成功的规则 ``llm_reviewed=True`` +
            ``note`` 由 LLM 回填；降级的规则保留原 note + 追加降级标记。
        """
        new_rules: list[RuleResult] = []
        for rule in check_result.rules:
            if rule.severity not in _REVIEWABLE_SEVERITIES:
                # INFO / CRITICAL 跳过；INFO 已通过、CRITICAL 缺数据无意义。
                new_rules.append(rule)
                continue

            reviewed = await self._review_one(rule, snapshot)
            new_rules.append(reviewed)

        # confidence 不变（LLM 复核只解释差异，不改通过/失败状态）。
        return check_result.model_copy(update={"rules": new_rules})

    async def _review_one(
        self,
        rule: RuleResult,
        snapshot: StatementSnapshot,
    ) -> RuleResult:
        """复核单条规则。

        Args:
            rule: 待复核的规则（已确认 ``severity ∈ {WARN, ERROR}``）。
            snapshot: 三表快照。

        Returns:
            复核后的新 ``RuleResult``；任何失败都降级为追加标记，不抛异常。
        """
        prompt = build_review_prompt(rule, snapshot)
        try:
            gen_result = await asyncio.to_thread(
                self.hub.generate,
                prompt,
                max_new_tokens=self.max_new_tokens,
                temperature=self.temperature,
                timeout_seconds=self.timeout_seconds,
            )
        except TimeoutError as exc:  # asyncio.to_thread 超时（罕见）
            LOGGER.warning(
                "[LLMReviewer._review_one] rule=%s 超时: %s",
                rule.rule_type.value,
                exc,
            )
            return rule.model_copy(
                update={
                    "note": _append_fallback(rule.note, "LLM 复核超时"),
                    "llm_reviewed": False,
                }
            )
        except Exception as exc:  # noqa: BLE001 — 任何异常都降级
            LOGGER.warning(
                "[LLMReviewer._review_one] rule=%s LLM 调用失败: %s",
                rule.rule_type.value,
                exc,
            )
            return rule.model_copy(
                update={
                    "note": _append_fallback(rule.note, f"LLM 复核失败: {exc}"),
                    "llm_reviewed": False,
                }
            )

        parsed = _extract_json_object(gen_result.text)
        if parsed is None or not isinstance(parsed, dict):
            LOGGER.warning(
                "[LLMReviewer._review_one] rule=%s LLM 输出无法解析 JSON: %.200s",
                rule.rule_type.value,
                gen_result.text,
            )
            return rule.model_copy(
                update={
                    "note": _append_fallback(rule.note, "LLM 输出无法解析为 JSON"),
                    "llm_reviewed": False,
                }
            )

        reason = str(parsed.get("reason", "")).strip()
        is_explained = bool(parsed.get("is_explained", True))

        if not reason:
            return rule.model_copy(
                update={
                    "note": _append_fallback(rule.note, "LLM 返回空 reason"),
                    "llm_reviewed": False,
                }
            )

        # 拼接最终 note：原硬编码提示 + LLM 解释。
        prefix = f"{rule.note} " if rule.note else ""
        suffix = "" if is_explained else "（LLM 也无法解释，建议人工排查）"
        final_note = f"{prefix}[LLM 复核] {reason}{suffix}"

        LOGGER.info(
            "[LLMReviewer._review_one] rule=%s is_explained=%s latency_ms=%.1f "
            "completion_tokens=%d",
            rule.rule_type.value,
            is_explained,
            gen_result.latency_ms,
            gen_result.completion_tokens,
        )

        return rule.model_copy(
            update={
                "note": final_note,
                "llm_reviewed": True,
            }
        )


# ============================================================================
# 辅助函数
# ============================================================================


def _append_fallback(original: str, fallback: str) -> str:
    """拼接降级标记到原 note。

    Args:
        original: 原始 note（可能为空）。
        fallback: 降级原因。

    Returns:
        ``"{original} [{fallback}]"`` 或 ``"[{fallback}]"``。
    """
    return f"{original} [{fallback}]" if original else f"[{fallback}]"


def _extract_json_object(text: str) -> Any:
    """从模型输出中提取首个 JSON 对象。

    顺序尝试（对齐 ``Extractor._extract_json_object``）：

    1. 直接 ``json.loads`` 去空白后的整段文本。
    2. 解包 ```json ... ``` 围栏后重试。
    3. 贪婪匹配首个 ``{...}`` 子串后重试。

    Args:
        text: 模型原始输出。

    Returns:
        解析后的 dict，或 ``None``。
    """
    candidate = text.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    fence_match = _FENCE_RE.search(text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    object_match = _OBJECT_RE.search(text)
    if object_match:
        try:
            return json.loads(object_match.group(0))
        except json.JSONDecodeError:
            pass

    return None


__all__ = ["LLMReviewer", "build_review_prompt"]
