"""M7 Extractor: 7B general-prompt baseline for three-statement extraction.

Spec §2.3 M7 + plan M2.06:

* ``Extractor.extract`` builds a JSON-constrained prompt via
  ``build_extract_prompt``, calls ``ModelHub.generate``, and parses
  the model output into a ``FinancialStatement``.
* JSON parse failures are NOT raised — they return
  ``ExtractionResult(success=False, error=...)`` so the M2.07 validator
  can retry with ``build_retry_prompt`` (temp=0.1) and fall back to
  the 7B-emit-correction path (spec §10.3).
* The Extractor does NOT acquire the ``model_lock`` itself — that is
  the caller's responsibility (``VramScheduler.load_for_scene_with_lock``
  in the MQ handler), so the class stays single-purpose and unit-testable
  with a mock ``ModelHub``.

Scene choice: M2.06 ships the 7B general-prompt baseline
(``Scene.REASON`` per ``app.modules.modelhub.modelhub``); the caller
loads the 7B before invoking ``extract``. The M4 T1 1.5B QLoRA swap
will switch the caller to ``Scene.EXTRACT`` without touching the
``Extractor`` class itself.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.modules.extractor.prompts import build_extract_prompt
from app.modules.modelhub.modelhub import ModelHub
from app.schemas.statement import (
    ExtractionResult,
    FinancialStatement,
    StatementItem,
    StatementType,
)
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# Strip ```json ... ``` fenced code blocks (Qwen sometimes wraps output).
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
# Capture the first balanced-looking object substring as last-resort fallback.
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class Extractor:
    """Three-statement extractor backed by ``ModelHub.generate``.

    The extractor is stateless beyond the held ``ModelHub`` reference;
    each ``extract`` call builds a fresh prompt and runs one generation.
    """

    def __init__(
        self,
        hub: ModelHub,
        *,
        default_max_new_tokens: int | None = None,
        default_temperature: float = 0.0,
        default_timeout_seconds: float | None = None,
    ) -> None:
        """Configure the extractor.

        Args:
            hub: ModelHub instance (must have an LLM loaded before
                ``extract`` is called; the caller is responsible for
                ``load_for_scene`` + ``model_lock``).
            default_max_new_tokens: Override ``Settings.model_max_new_tokens``.
            default_temperature: Default sampling temperature (0 = greedy).
            default_timeout_seconds: Override ``Settings.model_generate_timeout_seconds``.
        """
        self.hub = hub
        self.settings = hub.settings
        self.default_max_new_tokens = (
            default_max_new_tokens
            if default_max_new_tokens is not None
            else self.settings.model_max_new_tokens
        )
        self.default_temperature = default_temperature
        self.default_timeout_seconds = (
            default_timeout_seconds
            if default_timeout_seconds is not None
            else self.settings.model_generate_timeout_seconds
        )

    def extract(
        self,
        table_html: str,
        statement_type: StatementType,
        *,
        report_period: str = "",
        company_code: str = "",
        unit: str = "元",
        max_new_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
    ) -> ExtractionResult:
        """Extract one statement table into a ``FinancialStatement``.

        The caller is responsible for loading the right LLM via
        ``VramScheduler.load_for_scene_with_lock(Scene.REASON)`` (M2.06
        baseline) or ``Scene.EXTRACT`` (M4 T1 1.5B QLoRA path) before
        calling this method.

        Args:
            table_html: Raw HTML/Markdown table markup from the parser.
            statement_type: Which statement to extract.
            report_period: Optional ``YYYY-MM-DD`` report end date.
            company_code: Optional A-share ticker.
            unit: Unit hint for the value field.
            max_new_tokens: Override default max tokens.
            temperature: Override default temperature.
            timeout_seconds: Override default SLA timeout.

        Returns:
            An ``ExtractionResult``; check ``.success`` to decide
            whether to use ``.statement`` or surface ``.error``.

        Raises:
            ValueError: When ``table_html`` is empty.
            AiException: When the underlying ``ModelHub.generate`` fails
                (timeout / OOM mapped upstream).
        """
        if not table_html or not table_html.strip():
            raise ValueError("table_html must not be empty")

        prompt = build_extract_prompt(
            table_html,
            statement_type,
            report_period=report_period,
            company_code=company_code,
            unit=unit,
        )
        gen_result = self.hub.generate(
            prompt,
            max_new_tokens=max_new_tokens or self.default_max_new_tokens,
            temperature=(
                self.default_temperature if temperature is None else temperature
            ),
            timeout_seconds=(
                self.default_timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            ),
        )
        raw_text = gen_result.text
        LOGGER.info(
            "[Extractor.extract] type=%s prompt_tokens=%d completion_tokens=%d latency_ms=%.1f",
            statement_type.value,
            gen_result.prompt_tokens,
            gen_result.completion_tokens,
            gen_result.latency_ms,
        )
        return self._parse(
            raw_text,
            statement_type=statement_type,
            prompt_tokens=gen_result.prompt_tokens,
            completion_tokens=gen_result.completion_tokens,
            latency_ms=gen_result.latency_ms,
        )

    def extract_with_prompt(
        self,
        prompt: str,
        statement_type: StatementType,
        *,
        max_new_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
    ) -> ExtractionResult:
        """Run generation + parse on a pre-built prompt (M2.07 retry entry).

        Used by ``extract_with_retry`` when the first attempt fails
        validation: the caller rebuilds the prompt with
        ``build_retry_prompt`` and a lower temperature, then calls this
        method to re-generate without re-building the prompt.

        The caller is still responsible for the model_lock; this method
        only covers the generate + parse leg.

        Args:
            prompt: Pre-built prompt string (typically a retry prompt
                from ``build_retry_prompt``).
            statement_type: Target statement type for the result envelope.
            max_new_tokens: Override default max tokens.
            temperature: Override default temperature (spec §10.3 retry
                uses 0.1).
            timeout_seconds: Override default SLA timeout.

        Returns:
            An ``ExtractionResult``; check ``.success`` for parse outcome.

        Raises:
            AiException: When the underlying ``ModelHub.generate`` fails.
        """
        gen_result = self.hub.generate(
            prompt,
            max_new_tokens=max_new_tokens or self.default_max_new_tokens,
            temperature=(
                self.default_temperature if temperature is None else temperature
            ),
            timeout_seconds=(
                self.default_timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            ),
        )
        raw_text = gen_result.text
        LOGGER.info(
            "[Extractor.extract_with_prompt] type=%s prompt_tokens=%d "
            "completion_tokens=%d latency_ms=%.1f",
            statement_type.value,
            gen_result.prompt_tokens,
            gen_result.completion_tokens,
            gen_result.latency_ms,
        )
        return self._parse(
            raw_text,
            statement_type=statement_type,
            prompt_tokens=gen_result.prompt_tokens,
            completion_tokens=gen_result.completion_tokens,
            latency_ms=gen_result.latency_ms,
        )

    def _parse(
        self,
        raw_text: str,
        *,
        statement_type: StatementType,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
    ) -> ExtractionResult:
        """Parse the model output into a ``FinancialStatement``.

        Args:
            raw_text: Raw model output text.
            statement_type: Statement type for the result envelope.
            prompt_tokens: Token count from the generation result.
            completion_tokens: Token count from the generation result.
            latency_ms: End-to-end latency in milliseconds.

        Returns:
            ``ExtractionResult`` with ``success=True`` on parse + validate,
            otherwise ``success=False`` with the parse error.
        """
        common_kwargs: dict[str, Any] = {
            "statement_type": statement_type,
            "raw_text": raw_text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
        }
        if not raw_text or not raw_text.strip():
            return ExtractionResult(**common_kwargs, error="empty model output")

        parsed = self._extract_json_object(raw_text)
        if parsed is None:
            return ExtractionResult(
                **common_kwargs,
                error="no JSON object found in model output",
            )

        try:
            statement = self._coerce_to_statement(parsed, statement_type)
        except Exception as error:  # noqa: BLE001 - surfaced via result
            LOGGER.warning(
                "[Extractor._parse] validation failed type=%s error=%s",
                statement_type.value,
                error,
            )
            return ExtractionResult(
                **common_kwargs, error=f"schema validation failed: {error}"
            )

        return ExtractionResult(**common_kwargs, statement=statement)

    @staticmethod
    def _extract_json_object(text: str) -> Any:
        """Find and parse the first JSON object in ``text``.

        Tries, in order:
        1. Direct ``json.loads`` on the trimmed text.
        2. Unwrap a ```json ... ``` fenced block and retry.
        3. Greedy match the first ``{...}`` substring and retry.

        Args:
            text: Raw model output text.

        Returns:
            The parsed JSON object (dict), or ``None`` when no object
            could be parsed.
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

    @staticmethod
    def _coerce_to_statement(
        parsed: Any,
        expected_type: StatementType,
    ) -> FinancialStatement:
        """Validate the parsed dict against ``FinancialStatement``.

        Args:
            parsed: Parsed JSON object (typically a dict).
            expected_type: The statement type the caller requested; used
                to filter the ``statements`` dict so only the expected
                type is kept (the model sometimes emits all three).

        Returns:
            A validated ``FinancialStatement``.

        Raises:
            ValueError: When the dict shape does not match the schema.
        """
        if not isinstance(parsed, dict):
            raise ValueError(f"expected JSON object, got {type(parsed).__name__}")

        report_period = parsed.get("report_period")
        if not report_period or not isinstance(report_period, str):
            raise ValueError("report_period must be a non-empty string")

        currency = parsed.get("currency", "CNY")
        unit = parsed.get("unit", "元")
        raw_statements = parsed.get("statements") or {}

        if not isinstance(raw_statements, dict):
            raise ValueError("statements must be an object")

        # Accept either the expected type only or all three; we keep only
        # the requested type to keep downstream consumers simple.
        raw_items = (
            raw_statements.get(expected_type.value)
            or raw_statements.get(expected_type.name)
            or []
        )
        if not isinstance(raw_items, list):
            raise ValueError(f"statements[{expected_type.value}] must be a list")

        items: list[StatementItem] = []
        for idx, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, dict):
                raise ValueError(
                    f"statements[{expected_type.value}][{idx}] must be an object"
                )
            items.append(StatementItem.model_validate(raw_item))

        return FinancialStatement(
            report_period=report_period,
            currency=currency,
            unit=unit,
            statements={expected_type: items},
        )


__all__ = ["Extractor"]
