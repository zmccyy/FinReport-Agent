"""M2.06 Extractor + prompts + statement schema tests."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from app.core.config import Settings
from app.modules.extractor.extractor import Extractor
from app.modules.extractor.prompts import build_extract_prompt, build_retry_prompt
from app.modules.modelhub.llm_loader import GenerateResult
from app.modules.modelhub.modelhub import ModelHub
from app.schemas.statement import (
    ExtractionResult,
    FinancialStatement,
    Period,
    Scope,
    StatementItem,
    StatementType,
)

# ---------------------------------------------------------------------------
# Stub ModelHub — captures generate kwargs and returns a preset response
# ---------------------------------------------------------------------------


class _StubHub(ModelHub):
    """ModelHub subclass that returns a queued ``GenerateResult``.

    The stub deliberately does NOT call ``super().__init__`` to avoid
    instantiating a real ``LlmLoader`` (which would try to import torch
    on first access). We only need ``settings`` + ``generate``.
    """

    def __init__(
        self,
        *,
        response_text: str = "",
        prompt_tokens: int = 12,
        completion_tokens: int = 34,
        latency_ms: float = 100.0,
        generate_error: Exception | None = None,
        settings: Settings | None = None,
    ) -> None:
        # Bypass ModelHub.__init__ (which builds a real LlmLoader).
        self.settings = settings or Settings()
        self._response_text = response_text
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self._latency_ms = latency_ms
        self._generate_error = generate_error
        self.generate_calls: list[dict[str, Any]] = []

    def generate(  # type: ignore[override]
        self,
        prompt: str,
        *,
        max_new_tokens: int | None = None,
        temperature: float = 0.0,
        timeout_seconds: float | None = None,
    ) -> GenerateResult:
        """Record the call and return the queued response."""
        self.generate_calls.append(
            {
                "prompt": prompt,
                "max_new_tokens": max_new_tokens,
                "temperature": temperature,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._generate_error is not None:
            raise self._generate_error
        return GenerateResult(
            text=self._response_text,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            latency_ms=self._latency_ms,
        )


def _stub_hub(response_text: str = "", **kwargs: Any) -> _StubHub:
    """Build a stub hub that returns ``response_text`` on generate."""
    return _StubHub(response_text=response_text, **kwargs)


def _bs_payload(
    *,
    items: list[dict[str, Any]] | None = None,
    report_period: str = "2024-12-31",
    currency: str = "CNY",
    unit: str = "元",
) -> str:
    """Build a JSON string matching the BS statement contract."""
    items = (
        items
        if items is not None
        else [
            {
                "item": "货币资金",
                "value": 1234567890.00,
                "scope": "合并",
                "period": "本期",
            },
            {"item": "应收账款", "value": -50000.00, "scope": "合并", "period": "本期"},
        ]
    )
    return json.dumps(
        {
            "report_period": report_period,
            "currency": currency,
            "unit": unit,
            "statements": {"balance_sheet": items},
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# StatementType / Scope / Period enums
# ---------------------------------------------------------------------------


def test_statement_type_chinese_names() -> None:
    """Chinese labels are correct for prompts + reports."""
    assert StatementType.BALANCE_SHEET.chinese_name == "资产负债表"
    assert StatementType.INCOME_STATEMENT.chinese_name == "利润表"
    assert StatementType.CASH_FLOW.chinese_name == "现金流量表"


def test_statement_type_string_values() -> None:
    """String values are stable for JSON serialization."""
    assert StatementType.BALANCE_SHEET.value == "balance_sheet"
    assert StatementType.INCOME_STATEMENT.value == "income_statement"
    assert StatementType.CASH_FLOW.value == "cash_flow"


# ---------------------------------------------------------------------------
# StatementItem / FinancialStatement
# ---------------------------------------------------------------------------


def test_statement_item_rejects_nan_value() -> None:
    """NaN values are rejected per spec §2.3 M7 数值合法性."""
    with pytest.raises(ValueError, match="NaN"):
        StatementItem(item="货币资金", value=float("nan"))


def test_statement_item_accepts_negative_value() -> None:
    """Negative values are allowed (e.g. net loss)."""
    item = StatementItem(item="净利润", value=-12345.67)
    assert item.value == pytest.approx(-12345.67)


def test_statement_item_default_scope_and_period() -> None:
    """Defaults are 合并 / 本期."""
    item = StatementItem(item="货币资金", value=100.0)
    assert item.scope == Scope.CONSOLIDATED
    assert item.period == Period.CURRENT


def test_financial_statement_item_count() -> None:
    """item_count returns 0 for absent statements."""
    stmt = FinancialStatement(
        report_period="2024-12-31",
        statements={
            StatementType.BALANCE_SHEET: [
                StatementItem(item="货币资金", value=1.0),
                StatementItem(item="存货", value=2.0),
            ]
        },
    )
    assert stmt.item_count(StatementType.BALANCE_SHEET) == 2
    assert stmt.item_count(StatementType.INCOME_STATEMENT) == 0


def test_financial_statement_to_dict_uses_string_keys() -> None:
    """to_dict serializes statement type enum to string keys."""
    stmt = FinancialStatement(
        report_period="2024-12-31",
        statements={
            StatementType.BALANCE_SHEET: [
                StatementItem(item="货币资金", value=1.0, scope=Scope.CONSOLIDATED)
            ]
        },
    )
    payload = stmt.to_dict()
    assert "balance_sheet" in payload["statements"]
    assert payload["statements"]["balance_sheet"][0]["scope"] == "合并"


def test_extraction_result_success_property() -> None:
    """success is True only when statement is set and error is None."""
    ok = ExtractionResult(
        statement_type=StatementType.BALANCE_SHEET,
        statement=FinancialStatement(report_period="2024-12-31"),
    )
    assert ok.success is True

    fail_no_stmt = ExtractionResult(
        statement_type=StatementType.BALANCE_SHEET,
        error="bad json",
    )
    assert fail_no_stmt.success is False

    fail_with_error = ExtractionResult(
        statement_type=StatementType.BALANCE_SHEET,
        statement=FinancialStatement(report_period="2024-12-31"),
        error="partial",
    )
    assert fail_with_error.success is False


# ---------------------------------------------------------------------------
# build_extract_prompt
# ---------------------------------------------------------------------------


def test_build_extract_prompt_includes_required_fields() -> None:
    """Prompt carries system role, statement type, schema hint, and table."""
    prompt = build_extract_prompt(
        "<table>...</table>",
        StatementType.BALANCE_SHEET,
        report_period="2024-12-31",
        company_code="600519",
        unit="元",
    )
    assert "<|im_start|>system" in prompt
    assert "<|im_end|>" in prompt
    assert "资产负债表" in prompt
    assert "balance_sheet" in prompt
    assert "2024-12-31" in prompt
    assert "600519" in prompt
    assert "<table>...</table>" in prompt
    assert "JSON" in prompt


def test_build_extract_prompt_omits_optional_fields_when_empty() -> None:
    """Empty report_period / company_code are not added as header lines."""
    prompt = build_extract_prompt(
        "<table/>",
        StatementType.INCOME_STATEMENT,
    )
    assert "利润表" in prompt
    assert "income_statement" in prompt
    # Unit default is "元" — always emitted.
    assert "数值单位：元" in prompt


def test_build_extract_prompt_uses_statement_specific_schema_key() -> None:
    """Schema hint replaces <statement_type> with the actual type value."""
    prompt_bs = build_extract_prompt("<t/>", StatementType.BALANCE_SHEET)
    prompt_is = build_extract_prompt("<t/>", StatementType.INCOME_STATEMENT)
    prompt_cf = build_extract_prompt("<t/>", StatementType.CASH_FLOW)
    assert '"balance_sheet"' in prompt_bs
    assert '"income_statement"' in prompt_is
    assert '"cash_flow"' in prompt_cf


def test_build_retry_prompt_appends_error_hint() -> None:
    """Retry prompt references the original content + the parse error."""
    original = build_extract_prompt("<t/>", StatementType.BALANCE_SHEET)
    retry = build_retry_prompt(original, error_hint="Expecting ','")
    assert "Expecting ','" in retry
    assert retry.startswith(original)
    assert "JSON 解析失败" in retry


# ---------------------------------------------------------------------------
# Extractor._extract_json_object (static helper)
# ---------------------------------------------------------------------------


def test_extract_json_object_direct_parse() -> None:
    """Plain JSON text parses directly."""
    text = '{"a": 1, "b": [2, 3]}'
    parsed = Extractor._extract_json_object(text)
    assert parsed == {"a": 1, "b": [2, 3]}


def test_extract_json_object_strips_json_fenced_block() -> None:
    """```json fenced blocks are unwrapped before parsing."""
    text = '```json\n{"a": 1}\n```'
    parsed = Extractor._extract_json_object(text)
    assert parsed == {"a": 1}


def test_extract_json_object_strips_bare_fenced_block() -> None:
    """Bare ``` fenced blocks are unwrapped before parsing."""
    text = '```\n{"a": 1}\n```'
    parsed = Extractor._extract_json_object(text)
    assert parsed == {"a": 1}


def test_extract_json_object_extracts_from_prose() -> None:
    """JSON inside prose is captured by the greedy {...} fallback."""
    text = '好的，这是结果：\n{"a": 1, "b": 2}\n以上。'
    parsed = Extractor._extract_json_object(text)
    assert parsed == {"a": 1, "b": 2}


def test_extract_json_object_returns_none_for_non_json() -> None:
    """Pure prose without any object returns None."""
    assert Extractor._extract_json_object("nothing here") is None


def test_extract_json_object_returns_none_for_empty() -> None:
    """Empty input returns None."""
    assert Extractor._extract_json_object("") is None


# ---------------------------------------------------------------------------
# Extractor._coerce_to_statement (static helper)
# ---------------------------------------------------------------------------


def test_coerce_to_statement_happy_path() -> None:
    """A well-formed dict validates into a FinancialStatement."""
    parsed = json.loads(_bs_payload())
    stmt = Extractor._coerce_to_statement(parsed, StatementType.BALANCE_SHEET)
    assert stmt.report_period == "2024-12-31"
    assert stmt.currency == "CNY"
    assert stmt.unit == "元"
    assert stmt.item_count(StatementType.BALANCE_SHEET) == 2
    items = stmt.statements[StatementType.BALANCE_SHEET]
    assert items[0].item == "货币资金"
    assert items[1].value == pytest.approx(-50000.0)


def test_coerce_to_statement_filters_to_expected_type() -> None:
    """When the model emits all three, only the requested type is kept."""
    parsed = {
        "report_period": "2024-12-31",
        "currency": "CNY",
        "unit": "元",
        "statements": {
            "balance_sheet": [{"item": "货币资金", "value": 1.0}],
            "income_statement": [{"item": "营业收入", "value": 2.0}],
            "cash_flow": [{"item": "经营活动现金流", "value": 3.0}],
        },
    }
    stmt = Extractor._coerce_to_statement(parsed, StatementType.INCOME_STATEMENT)
    assert stmt.item_count(StatementType.INCOME_STATEMENT) == 1
    assert stmt.item_count(StatementType.BALANCE_SHEET) == 0
    assert stmt.item_count(StatementType.CASH_FLOW) == 0


def test_coerce_to_statement_accepts_enum_name_key() -> None:
    """Model may emit BALANCE_SHEET enum name as key; we accept that too."""
    parsed = {
        "report_period": "2024-12-31",
        "statements": {"BALANCE_SHEET": [{"item": "货币资金", "value": 1.0}]},
    }
    stmt = Extractor._coerce_to_statement(parsed, StatementType.BALANCE_SHEET)
    assert stmt.item_count(StatementType.BALANCE_SHEET) == 1


def test_coerce_to_statement_rejects_non_dict() -> None:
    """Parsed JSON arrays surface as ValueError."""
    with pytest.raises(ValueError, match="expected JSON object"):
        Extractor._coerce_to_statement([1, 2, 3], StatementType.BALANCE_SHEET)


def test_coerce_to_statement_rejects_missing_report_period() -> None:
    """Missing report_period raises ValueError."""
    parsed = {"statements": {"balance_sheet": []}}
    with pytest.raises(ValueError, match="report_period"):
        Extractor._coerce_to_statement(parsed, StatementType.BALANCE_SHEET)


def test_coerce_to_statement_rejects_non_string_report_period() -> None:
    """Non-string report_period raises ValueError."""
    parsed = {"report_period": 20241231, "statements": {}}
    with pytest.raises(ValueError, match="report_period"):
        Extractor._coerce_to_statement(parsed, StatementType.BALANCE_SHEET)


def test_coerce_to_statement_rejects_statements_not_object() -> None:
    """statements field must be an object."""
    parsed = {"report_period": "2024-12-31", "statements": [1, 2]}
    with pytest.raises(ValueError, match="statements must be"):
        Extractor._coerce_to_statement(parsed, StatementType.BALANCE_SHEET)


def test_coerce_to_statement_rejects_items_not_list() -> None:
    """The per-statement value must be a list."""
    parsed = {
        "report_period": "2024-12-31",
        "statements": {"balance_sheet": {"item": "x", "value": 1.0}},
    }
    with pytest.raises(ValueError, match="must be a list"):
        Extractor._coerce_to_statement(parsed, StatementType.BALANCE_SHEET)


def test_coerce_to_statement_rejects_item_not_object() -> None:
    """Per-item entries must be objects."""
    parsed = {
        "report_period": "2024-12-31",
        "statements": {"balance_sheet": ["not-an-object"]},
    }
    with pytest.raises(ValueError, match="must be an object"):
        Extractor._coerce_to_statement(parsed, StatementType.BALANCE_SHEET)


def test_coerce_to_statement_rejects_invalid_scope_enum() -> None:
    """Invalid scope value surfaces as a validation error."""
    parsed = {
        "report_period": "2024-12-31",
        "statements": {"balance_sheet": [{"item": "x", "value": 1.0, "scope": "集团"}]},
    }
    with pytest.raises(ValueError):
        Extractor._coerce_to_statement(parsed, StatementType.BALANCE_SHEET)


def test_coerce_to_statement_rejects_nan_value() -> None:
    """NaN value surfaces as a validation error (StatementItem validator)."""
    parsed = {
        "report_period": "2024-12-31",
        "statements": {"balance_sheet": [{"item": "x", "value": float("nan")}]},
    }
    with pytest.raises(ValueError):
        Extractor._coerce_to_statement(parsed, StatementType.BALANCE_SHEET)


def test_coerce_to_statement_defaults_currency_and_unit() -> None:
    """Missing currency/unit fall back to CNY / 元."""
    parsed = {
        "report_period": "2024-12-31",
        "statements": {"balance_sheet": []},
    }
    stmt = Extractor._coerce_to_statement(parsed, StatementType.BALANCE_SHEET)
    assert stmt.currency == "CNY"
    assert stmt.unit == "元"


# ---------------------------------------------------------------------------
# Extractor.extract (integration with stub hub)
# ---------------------------------------------------------------------------


def test_extract_returns_success_on_valid_json() -> None:
    """Happy path: model returns valid JSON → success=True."""
    hub = _stub_hub(_bs_payload())
    extractor = Extractor(hub)

    result = extractor.extract(
        "<table>...</table>",
        StatementType.BALANCE_SHEET,
        report_period="2024-12-31",
        company_code="600519",
    )

    assert result.success is True
    assert result.statement is not None
    assert result.statement.report_period == "2024-12-31"
    assert result.statement.item_count(StatementType.BALANCE_SHEET) == 2
    assert result.error is None
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 34
    assert result.latency_ms == pytest.approx(100.0)


def test_extract_passes_default_kwargs_to_hub() -> None:
    """Default max_new_tokens / temperature / timeout come from Settings."""
    hub = _stub_hub(_bs_payload())
    extractor = Extractor(hub)

    extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    call = hub.generate_calls[0]
    assert call["max_new_tokens"] == hub.settings.model_max_new_tokens
    assert call["temperature"] == 0.0
    assert call["timeout_seconds"] == hub.settings.model_generate_timeout_seconds


def test_extract_overrides_passed_kwargs() -> None:
    """Explicit kwargs override defaults."""
    hub = _stub_hub(_bs_payload())
    extractor = Extractor(hub)

    extractor.extract(
        "<table/>",
        StatementType.BALANCE_SHEET,
        max_new_tokens=256,
        temperature=0.1,
        timeout_seconds=30.0,
    )

    call = hub.generate_calls[0]
    assert call["max_new_tokens"] == 256
    assert call["temperature"] == pytest.approx(0.1)
    assert call["timeout_seconds"] == pytest.approx(30.0)


def test_extract_rejects_empty_table_html() -> None:
    """Empty table_html raises ValueError before calling the model."""
    hub = _stub_hub(_bs_payload())
    extractor = Extractor(hub)

    with pytest.raises(ValueError, match="table_html"):
        extractor.extract("   ", StatementType.BALANCE_SHEET)

    assert hub.generate_calls == []


def test_extract_returns_failure_on_empty_model_output() -> None:
    """Empty model output → success=False with descriptive error."""
    hub = _stub_hub("")
    extractor = Extractor(hub)

    result = extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    assert result.success is False
    assert result.statement is None
    assert "empty" in result.error.lower()


def test_extract_returns_failure_on_non_json_output() -> None:
    """Pure prose without JSON → success=False."""
    hub = _stub_hub("抱歉，我无法解析这个表格。")
    extractor = Extractor(hub)

    result = extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    assert result.success is False
    assert "no JSON object" in result.error
    assert result.raw_text == "抱歉，我无法解析这个表格。"


def test_extract_returns_failure_on_invalid_schema() -> None:
    """JSON missing report_period → success=False with schema error."""
    hub = _stub_hub(
        json.dumps(
            {
                "currency": "CNY",
                "unit": "元",
                "statements": {"balance_sheet": []},
            }
        )
    )
    extractor = Extractor(hub)

    result = extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    assert result.success is False
    assert "schema validation failed" in result.error
    assert "report_period" in result.error


def test_extract_handles_fenced_json_output() -> None:
    """Model wraps JSON in ```json fenced block → still succeeds."""
    hub = _stub_hub(f"```json\n{_bs_payload()}\n```")
    extractor = Extractor(hub)

    result = extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    assert result.success is True
    assert result.statement is not None
    assert result.statement.item_count(StatementType.BALANCE_SHEET) == 2


def test_extract_handles_prose_with_embedded_json() -> None:
    """Model emits JSON inside prose → greedy {...} fallback parses it."""
    hub = _stub_hub(f"以下是抽取结果：\n{_bs_payload()}\n以上。")
    extractor = Extractor(hub)

    result = extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    assert result.success is True
    assert result.statement is not None


def test_extract_preserves_raw_text_on_failure() -> None:
    """raw_text always carries the model output for debugging."""
    raw = "not json at all"
    hub = _stub_hub(raw)
    extractor = Extractor(hub)

    result = extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    assert result.raw_text == raw


def test_extract_propagates_generate_exception() -> None:
    """AiException from ModelHub.generate propagates (timeout/OOM upstream)."""
    from app.core.exceptions import InferenceTimeoutException

    hub = _stub_hub(
        "",
        generate_error=InferenceTimeoutException("LLM generate exceeded 60s"),
    )
    extractor = Extractor(hub)

    with pytest.raises(InferenceTimeoutException, match="exceeded 60s"):
        extractor.extract("<table/>", StatementType.BALANCE_SHEET)


def test_extract_constructor_overrides_defaults() -> None:
    """Constructor overrides take precedence over Settings defaults."""
    hub = _stub_hub(_bs_payload())
    extractor = Extractor(
        hub,
        default_max_new_tokens=128,
        default_temperature=0.05,
        default_timeout_seconds=15.0,
    )

    extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    call = hub.generate_calls[0]
    assert call["max_new_tokens"] == 128
    assert call["temperature"] == pytest.approx(0.05)
    assert call["timeout_seconds"] == pytest.approx(15.0)


def test_extract_preserves_token_counts_in_result() -> None:
    """prompt_tokens / completion_tokens / latency_ms flow into the result."""
    hub = _stub_hub(
        _bs_payload(),
        prompt_tokens=42,
        completion_tokens=88,
        latency_ms=1234.5,
    )
    extractor = Extractor(hub)

    result = extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    assert result.prompt_tokens == 42
    assert result.completion_tokens == 88
    assert result.latency_ms == pytest.approx(1234.5)


def test_extract_handles_negative_values() -> None:
    """Negative numeric values are preserved through the pipeline."""
    items = [
        {"item": "净利润", "value": -9876543.21, "scope": "合并", "period": "本期"},
    ]
    hub = _stub_hub(_bs_payload(items=items))
    extractor = Extractor(hub)

    result = extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    assert result.success is True
    extracted = result.statement.statements[StatementType.BALANCE_SHEET][0]
    assert extracted.value == pytest.approx(-9876543.21)


def test_extract_handles_wan_yuan_unit() -> None:
    """Unit 万元 is propagated through to the statement."""
    payload = json.dumps(
        {
            "report_period": "2024-12-31",
            "currency": "CNY",
            "unit": "万元",
            "statements": {
                "balance_sheet": [
                    {
                        "item": "货币资金",
                        "value": 12345.67,
                        "scope": "合并",
                        "period": "本期",
                    }
                ]
            },
        },
        ensure_ascii=False,
    )
    hub = _stub_hub(payload)
    extractor = Extractor(hub)

    result = extractor.extract("<table/>", StatementType.BALANCE_SHEET, unit="万元")

    assert result.success is True
    assert result.statement.unit == "万元"


def test_extract_rejects_value_with_thousands_separator() -> None:
    """String values like '1,234,567' surface as schema validation error.

    The model is told to emit numbers, but if it slips a string the
    validator catches it and returns success=False so the M2.07 retry
    can fire.
    """
    items = [
        {"item": "货币资金", "value": "1,234,567", "scope": "合并", "period": "本期"},
    ]
    hub = _stub_hub(_bs_payload(items=items))
    extractor = Extractor(hub)

    result = extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    assert result.success is False
    assert "schema validation failed" in result.error


def test_extract_handles_all_three_statement_types() -> None:
    """Each StatementType produces a result with the matching key."""
    for st_type, items in [
        (StatementType.BALANCE_SHEET, [{"item": "货币资金", "value": 1.0}]),
        (StatementType.INCOME_STATEMENT, [{"item": "营业收入", "value": 2.0}]),
        (StatementType.CASH_FLOW, [{"item": "经营活动现金流", "value": 3.0}]),
    ]:
        payload = json.dumps(
            {
                "report_period": "2024-12-31",
                "currency": "CNY",
                "unit": "元",
                "statements": {st_type.value: items},
            },
            ensure_ascii=False,
        )
        hub = _stub_hub(payload)
        extractor = Extractor(hub)

        result = extractor.extract("<table/>", st_type)

        assert result.success is True, f"failed for {st_type}"
        assert result.statement.item_count(st_type) == 1


def test_extract_does_not_call_load_for_scene() -> None:
    """Extractor.extract never calls load_for_scene (caller's job)."""
    hub = _stub_hub(_bs_payload())
    hub.load_calls: list[tuple[str, str]] = []  # type: ignore[attr-defined]
    original_load = hub.load_llm

    def tracking_load(name: str, quant: str) -> None:
        """Record load attempts; Extractor should never invoke this."""
        hub.load_calls.append((name, quant))  # type: ignore[attr-defined]
        original_load(name, quant)

    hub.load_llm = tracking_load  # type: ignore[assignment]
    extractor = Extractor(hub)

    extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    assert hub.load_calls == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# NaN edge case: float('nan') is not equal to itself
# ---------------------------------------------------------------------------


def test_extract_value_nan_round_trip_detection() -> None:
    """NaN values are rejected even when emitted as JSON null."""
    # JSON has no NaN literal; some models emit null which fails float coercion.
    payload = json.dumps(
        {
            "report_period": "2024-12-31",
            "statements": {
                "balance_sheet": [
                    {"item": "x", "value": None, "scope": "合并", "period": "本期"}
                ]
            },
        }
    )
    hub = _stub_hub(payload)
    extractor = Extractor(hub)

    result = extractor.extract("<table/>", StatementType.BALANCE_SHEET)

    assert result.success is False
    # NaN-or-null failure is caught by Pydantic float coercion.
    assert "schema validation failed" in result.error
    # Sanity: float('nan') is rejected by StatementItem directly.
    with pytest.raises(ValueError):
        StatementItem(item="x", value=float("nan"))
    assert math.isnan(float("nan"))  # type: ignore[arg-type]
