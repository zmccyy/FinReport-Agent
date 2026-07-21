"""M2.07 Validator + extract_with_retry tests (spec §2.3 M7 + §10.3 降级链)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.config import Settings
from app.modules.extractor.extractor import Extractor
from app.modules.extractor.prompts import build_extract_prompt, build_retry_prompt
from app.modules.extractor.validator import (
    ValidationIssue,
    ValidationResult,
    Validator,
    extract_with_retry,
)
from app.modules.modelhub.llm_loader import GenerateResult
from app.modules.modelhub.modelhub import ModelHub
from app.schemas.statement import (
    ExtractionResult,
    FinancialStatement,
    StatementItem,
    StatementType,
)

# ---------------------------------------------------------------------------
# Stub ModelHub (mirrors test_m2_extractor._StubHub, kept local to avoid
# cross-file test coupling)
# ---------------------------------------------------------------------------


class _StubHub(ModelHub):
    """ModelHub subclass that returns queued responses per call.

    ``responses`` is a list consumed in order; each call to ``generate``
    pops the front. This lets retry tests queue a bad-then-good response.
    """

    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        prompt_tokens: int = 12,
        completion_tokens: int = 34,
        latency_ms: float = 100.0,
        generate_error: Exception | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self._responses = list(responses) if responses else []
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
        """Record the call and return the next queued response."""
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
        text = self._responses.pop(0) if self._responses else ""
        return GenerateResult(
            text=text,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            latency_ms=self._latency_ms,
        )


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


def _make_result(
    response_text: str,
    *,
    statement_type: StatementType = StatementType.BALANCE_SHEET,
) -> ExtractionResult:
    """Run Extractor.extract on a stub hub and return the result."""
    hub = _StubHub(responses=[response_text])
    extractor = Extractor(hub)
    return extractor.extract("<table></table>", statement_type)


# ---------------------------------------------------------------------------
# ValidationIssue / ValidationResult model tests
# ---------------------------------------------------------------------------


def test_validation_issue_defaults_path_empty() -> None:
    """Path defaults to empty string when not specified."""
    issue = ValidationIssue(severity="error", code="x", message="m")
    assert issue.path == ""


def test_validation_result_error_count_filters_severity() -> None:
    """error_count only counts error-severity issues."""
    vr = ValidationResult(
        is_valid=False,
        issues=[
            ValidationIssue(severity="error", code="a", message="m1"),
            ValidationIssue(severity="warning", code="b", message="m2"),
            ValidationIssue(severity="error", code="c", message="m3"),
        ],
    )
    assert vr.error_count == 2
    assert vr.warning_count == 1


def test_validation_result_default_empty() -> None:
    """Default ValidationResult has no issues and zero counts."""
    vr = ValidationResult(is_valid=True)
    assert vr.issues == []
    assert vr.error_count == 0
    assert vr.warning_count == 0


# ---------------------------------------------------------------------------
# Validator.validate — parse-failed path
# ---------------------------------------------------------------------------


def test_validate_returns_parse_failed_when_result_not_successful() -> None:
    """A failed ExtractionResult surfaces as a parse_failed error issue."""
    result = ExtractionResult(
        statement_type=StatementType.BALANCE_SHEET,
        error="no JSON object found in model output",
    )
    validator = Validator()

    vr = validator.validate(result)

    assert vr.is_valid is False
    assert vr.error_count == 1
    assert vr.issues[0].code == "parse_failed"
    assert "no JSON object" in vr.issues[0].message
    assert vr.error_hint == "no JSON object found in model output"


def test_validate_uses_default_error_when_result_error_is_none() -> None:
    """A falsy result.error yields a generic parse_failed message."""
    result = ExtractionResult(statement_type=StatementType.BALANCE_SHEET)
    validator = Validator()

    vr = validator.validate(result)

    assert vr.is_valid is False
    assert vr.issues[0].message == "parse failed"
    assert vr.error_hint == "parse failed"


# ---------------------------------------------------------------------------
# Validator.validate_statement — field-level rules
# ---------------------------------------------------------------------------


def _make_statement(
    *,
    report_period: str = "2024-12-31",
    currency: str = "CNY",
    unit: str = "元",
    items: list[StatementItem] | None = None,
    statement_type: StatementType = StatementType.BALANCE_SHEET,
) -> FinancialStatement:
    """Build a FinancialStatement with sensible defaults."""
    if items is None:
        items = [
            StatementItem(item="货币资金", value=100.0),
            StatementItem(item="应收账款", value=200.0),
        ]
    return FinancialStatement(
        report_period=report_period,
        currency=currency,
        unit=unit,
        statements={statement_type: items},
    )


def test_validate_statement_happy_path_no_issues() -> None:
    """A clean BS statement (with all required items) produces no issues."""
    validator = Validator()
    items = [
        StatementItem(item="货币资金", value=100.0),
        StatementItem(item="应收账款", value=200.0),
        StatementItem(item="资产总计", value=300.0),
        StatementItem(item="负债合计", value=150.0),
        StatementItem(item="所有者权益合计", value=150.0),
    ]
    issues = validator.validate_statement(_make_statement(items=items))
    assert issues == []


def test_validate_statement_rejects_bad_report_period_format() -> None:
    """report_period not in YYYY-MM-DD is an error."""
    validator = Validator()
    stmt = _make_statement(report_period="2024/12/31")
    issues = validator.validate_statement(stmt)
    assert any(
        i.code == "invalid_report_period" and i.severity == "error" for i in issues
    )


def test_validate_statement_warns_on_non_iso_currency() -> None:
    """currency not 3 letters is a warning (not blocking)."""
    validator = Validator()
    stmt = _make_statement(currency="人民币")
    issues = validator.validate_statement(stmt)
    currency_issues = [i for i in issues if i.code == "invalid_currency"]
    assert len(currency_issues) == 1
    assert currency_issues[0].severity == "warning"


def test_validate_statement_warns_on_unknown_unit() -> None:
    """unit outside the known enum is a warning."""
    validator = Validator()
    stmt = _make_statement(unit="亿美元")
    issues = validator.validate_statement(stmt)
    unit_issues = [i for i in issues if i.code == "unknown_unit"]
    assert len(unit_issues) == 1
    assert unit_issues[0].severity == "warning"


def test_validate_statement_rejects_empty_statements() -> None:
    """An empty statements dict is an error."""
    validator = Validator()
    stmt = FinancialStatement(report_period="2024-12-31", statements={})
    issues = validator.validate_statement(stmt)
    assert any(i.code == "empty_statements" and i.severity == "error" for i in issues)


def test_validate_statement_rejects_nan_value() -> None:
    """NaN value is an error (Pydantic bypassed via model_construct)."""
    validator = Validator()
    # Bypass Pydantic's _reject_nan validator by constructing without validation.
    item = StatementItem.model_construct(item="货币资金", value=float("nan"))
    stmt = _make_statement(items=[item])
    issues = validator.validate_statement(stmt)
    nan_issues = [i for i in issues if i.code == "nan_value"]
    assert len(nan_issues) == 1
    assert nan_issues[0].severity == "error"
    assert "货币资金" in nan_issues[0].message


def test_validate_statement_rejects_inf_value() -> None:
    """Infinite value is an error."""
    validator = Validator()
    item = StatementItem.model_construct(item="货币资金", value=float("inf"))
    stmt = _make_statement(items=[item])
    issues = validator.validate_statement(stmt)
    inf_issues = [i for i in issues if i.code == "inf_value"]
    assert len(inf_issues) == 1
    assert inf_issues[0].severity == "error"


def test_validate_statement_warns_on_out_of_range_value() -> None:
    """A value above 1e15 triggers a warning."""
    validator = Validator()
    item = StatementItem(item="资产总计", value=1e16)
    stmt = _make_statement(items=[item])
    issues = validator.validate_statement(stmt)
    range_issues = [i for i in issues if i.code == "value_out_of_range"]
    assert len(range_issues) == 1
    assert range_issues[0].severity == "warning"


def test_validate_statement_warns_on_duplicate_item() -> None:
    """Duplicate item names in the same statement produce a warning."""
    validator = Validator()
    items = [
        StatementItem(item="货币资金", value=1.0),
        StatementItem(item="货币资金", value=2.0),
    ]
    stmt = _make_statement(items=items)
    issues = validator.validate_statement(stmt)
    dup_issues = [i for i in issues if i.code == "duplicate_item"]
    assert len(dup_issues) == 1
    assert dup_issues[0].severity == "warning"
    assert "index 0" in dup_issues[0].message


def test_validate_statement_warns_on_missing_required_items_bs() -> None:
    """BS missing 资产总计/负债合计/所有者权益合计 produces a warning."""
    validator = Validator()
    # Only 货币资金 + 应收账款, no totals → all 3 required items missing.
    stmt = _make_statement()
    issues = validator.validate_statement(stmt)
    missing_issues = [i for i in issues if i.code == "missing_required_item"]
    assert len(missing_issues) == 1
    assert "资产总计" in missing_issues[0].message
    assert "负债合计" in missing_issues[0].message
    assert "所有者权益合计" in missing_issues[0].message


def test_validate_statement_no_missing_warning_when_required_present() -> None:
    """BS with all required items produces no missing_required_item warning."""
    validator = Validator()
    items = [
        StatementItem(item="货币资金", value=1.0),
        StatementItem(item="资产总计", value=2.0),
        StatementItem(item="负债合计", value=3.0),
        StatementItem(item="所有者权益合计", value=4.0),
    ]
    stmt = _make_statement(items=items)
    issues = validator.validate_statement(stmt)
    assert not any(i.code == "missing_required_item" for i in issues)


def test_validate_statement_missing_required_items_is() -> None:
    """IS missing 营业收入/净利润 produces a warning."""
    validator = Validator()
    items = [StatementItem(item="营业成本", value=100.0)]
    stmt = FinancialStatement(
        report_period="2024-12-31",
        statements={StatementType.INCOME_STATEMENT: items},
    )
    issues = validator.validate_statement(stmt)
    missing = [i for i in issues if i.code == "missing_required_item"]
    assert len(missing) == 1
    assert "营业收入" in missing[0].message
    assert "净利润" in missing[0].message


def test_validate_statement_missing_required_items_cf() -> None:
    """CF missing 经营活动产生的现金流量净额 produces a warning."""
    validator = Validator()
    items = [StatementItem(item="投资活动现金流入", value=50.0)]
    stmt = FinancialStatement(
        report_period="2024-12-31",
        statements={StatementType.CASH_FLOW: items},
    )
    issues = validator.validate_statement(stmt)
    missing = [i for i in issues if i.code == "missing_required_item"]
    assert len(missing) == 1
    assert "经营活动产生的现金流量净额" in missing[0].message


def test_validate_statement_warns_on_empty_statement_type_list() -> None:
    """An empty items list for a present statement type produces a warning."""
    validator = Validator()
    stmt = FinancialStatement(
        report_period="2024-12-31",
        statements={StatementType.BALANCE_SHEET: []},
    )
    issues = validator.validate_statement(stmt)
    empty_issues = [i for i in issues if i.code == "empty_statement_type"]
    assert len(empty_issues) == 1
    assert empty_issues[0].severity == "warning"


def test_validate_statement_path_locates_issue() -> None:
    """Issue path uses statements.{type}[idx].{field} format."""
    validator = Validator()
    item = StatementItem.model_construct(item="x", value=float("nan"))
    stmt = _make_statement(items=[item])
    issues = validator.validate_statement(stmt)
    nan_issue = next(i for i in issues if i.code == "nan_value")
    assert nan_issue.path == "statements.balance_sheet[0].value"


# ---------------------------------------------------------------------------
# Validator.validate — end-to-end on ExtractionResult
# ---------------------------------------------------------------------------


def test_validate_successful_result_no_errors() -> None:
    """A clean extraction result validates with no error issues."""
    result = _make_result(_bs_payload())
    assert result.success  # sanity check

    validator = Validator()
    vr = validator.validate(result)

    # Warnings for missing required items are expected; no errors.
    assert vr.is_valid is True
    assert vr.error_count == 0
    # May have warnings for missing required items, but error_hint must be empty.
    assert vr.error_hint == ""


def test_validate_successful_result_with_required_items_clean() -> None:
    """A BS with all required items validates with no issues at all."""
    payload = _bs_payload(
        items=[
            {"item": "货币资金", "value": 1.0, "scope": "合并", "period": "本期"},
            {"item": "资产总计", "value": 2.0, "scope": "合并", "period": "本期"},
            {"item": "负债合计", "value": 3.0, "scope": "合并", "period": "本期"},
            {"item": "所有者权益合计", "value": 4.0, "scope": "合并", "period": "本期"},
        ]
    )
    result = _make_result(payload)
    validator = Validator()
    vr = validator.validate(result)
    assert vr.is_valid is True
    assert vr.issues == []


def test_validate_collects_multiple_issues() -> None:
    """Multiple problems produce multiple issues; error_hint joins first 3."""
    # Bad report_period + unknown unit + missing required items.
    payload = _bs_payload(
        items=[{"item": "货币资金", "value": 1.0, "scope": "合并", "period": "本期"}],
        report_period="2024/12/31",
        unit="亿美元",
    )
    result = _make_result(payload)
    validator = Validator()
    vr = validator.validate(result)

    assert vr.is_valid is False
    codes = {i.code for i in vr.issues}
    assert "invalid_report_period" in codes
    assert "unknown_unit" in codes
    assert "missing_required_item" in codes
    # error_hint is built from issue *messages*, not codes.
    assert "report_period" in vr.error_hint or "YYYY-MM-DD" in vr.error_hint


# ---------------------------------------------------------------------------
# Validator.schema — lazy JSON Schema loading
# ---------------------------------------------------------------------------


def test_validator_schema_loads_from_default_path() -> None:
    """Validator.schema lazily loads statement_schema.json."""
    validator = Validator()
    schema = validator.schema
    assert isinstance(schema, dict)
    assert schema.get("title") == "FinancialStatement"
    assert schema.get("$schema") == "http://json-schema.org/draft-07/schema#"
    assert "properties" in schema
    assert "report_period" in schema["properties"]


def test_validator_schema_caches_after_first_load() -> None:
    """Repeated access returns the cached schema dict."""
    validator = Validator()
    first = validator.schema
    second = validator.schema
    assert first is second


def test_validator_schema_returns_empty_dict_on_missing_file() -> None:
    """A non-existent schema_path yields an empty dict, not an error."""
    from pathlib import Path

    validator = Validator(schema_path=Path("/nonexistent/statement_schema.json"))
    assert validator.schema == {}


# ---------------------------------------------------------------------------
# Extractor.extract_with_prompt — retry entry point
# ---------------------------------------------------------------------------


def test_extract_with_prompt_uses_pre_built_prompt_and_temp() -> None:
    """extract_with_prompt forwards a pre-built prompt with the given temp."""
    hub = _StubHub(responses=["{}"])
    extractor = Extractor(hub)
    retry_prompt = build_retry_prompt(
        build_extract_prompt("<table></table>", StatementType.BALANCE_SHEET),
        error_hint="bad json",
    )

    extractor.extract_with_prompt(
        retry_prompt,
        StatementType.BALANCE_SHEET,
        temperature=0.1,
        max_new_tokens=512,
    )

    assert len(hub.generate_calls) == 1
    call = hub.generate_calls[0]
    assert call["prompt"] == retry_prompt
    assert call["temperature"] == pytest.approx(0.1)
    assert call["max_new_tokens"] == 512


def test_extract_with_prompt_returns_extraction_result() -> None:
    """extract_with_prompt returns a parsed ExtractionResult."""
    hub = _StubHub(responses=[_bs_payload()])
    extractor = Extractor(hub)

    result = extractor.extract_with_prompt(
        "any prompt", StatementType.BALANCE_SHEET, temperature=0.1
    )

    assert result.success
    assert result.statement is not None
    assert result.statement.report_period == "2024-12-31"


# ---------------------------------------------------------------------------
# extract_with_retry — full retry flow (spec §10.3 降级链)
# ---------------------------------------------------------------------------


def test_extract_with_retry_returns_first_attempt_when_valid() -> None:
    """A valid first attempt skips retry and returns immediately."""
    hub = _StubHub(responses=[_bs_payload()])
    extractor = Extractor(hub)
    validator = Validator()

    result, validation = extract_with_retry(
        extractor,
        validator,
        "<table></table>",
        StatementType.BALANCE_SHEET,
    )

    assert validation.is_valid is True
    assert result.success
    assert len(hub.generate_calls) == 1  # no retry


def test_extract_with_retry_retries_on_validation_failure() -> None:
    """First attempt invalid → retry once with retry_temperature=0.1."""
    # First response: bad JSON; second: valid JSON.
    hub = _StubHub(responses=["not json at all", _bs_payload()])
    extractor = Extractor(hub)
    validator = Validator()

    result, validation = extract_with_retry(
        extractor,
        validator,
        "<table></table>",
        StatementType.BALANCE_SHEET,
        retry_temperature=0.1,
    )

    assert len(hub.generate_calls) == 2
    # Retry call used temp=0.1.
    assert hub.generate_calls[1]["temperature"] == pytest.approx(0.1)
    # Retry call used a prompt with the error hint appended.
    assert "上一次输出 JSON 解析失败" in hub.generate_calls[1]["prompt"]
    # Retry succeeded.
    assert validation.is_valid is True
    assert result.success


def test_extract_with_retry_returns_last_failure_when_retry_also_fails() -> None:
    """Both attempts invalid → return the retry result + validation."""
    hub = _StubHub(responses=["not json", "still not json"])
    extractor = Extractor(hub)
    validator = Validator()

    result, validation = extract_with_retry(
        extractor,
        validator,
        "<table></table>",
        StatementType.BALANCE_SHEET,
    )

    assert len(hub.generate_calls) == 2
    assert validation.is_valid is False
    assert validation.error_count >= 1
    assert not result.success
    assert "parse_failed" in {i.code for i in validation.issues}


def test_extract_with_retry_skips_retry_when_max_retries_zero() -> None:
    """max_retries=0 disables retry; returns first attempt only."""
    hub = _StubHub(responses=["not json"])
    extractor = Extractor(hub)
    validator = Validator()

    result, validation = extract_with_retry(
        extractor,
        validator,
        "<table></table>",
        StatementType.BALANCE_SHEET,
        max_retries=0,
    )

    assert len(hub.generate_calls) == 1
    assert validation.is_valid is False


def test_extract_with_retry_first_attempt_business_rule_error_triggers_retry() -> None:
    """A parse-success-but-business-rule-error result triggers retry.

    Constructs a response with invalid report_period so the validator
    emits an error code (invalid_report_period) and forces retry.
    """
    bad_payload = _bs_payload(report_period="2024/12/31")
    good_payload = _bs_payload(report_period="2024-12-31")
    hub = _StubHub(responses=[bad_payload, good_payload])
    extractor = Extractor(hub)
    validator = Validator()

    result, validation = extract_with_retry(
        extractor,
        validator,
        "<table></table>",
        StatementType.BALANCE_SHEET,
        retry_temperature=0.1,
    )

    assert len(hub.generate_calls) == 2
    assert validation.is_valid is True
    assert result.success
    assert result.statement.report_period == "2024-12-31"


def test_extract_with_retry_passes_overrides_to_first_attempt() -> None:
    """max_new_tokens / timeout_seconds are forwarded to both attempts."""
    hub = _StubHub(responses=[_bs_payload()])
    extractor = Extractor(hub)
    validator = Validator()

    extract_with_retry(
        extractor,
        validator,
        "<table></table>",
        StatementType.BALANCE_SHEET,
        max_new_tokens=777,
        timeout_seconds=42.0,
    )

    call = hub.generate_calls[0]
    assert call["max_new_tokens"] == 777
    assert call["timeout_seconds"] == pytest.approx(42.0)


def test_extract_with_retry_uses_custom_retry_temperature() -> None:
    """retry_temperature override flows to the retry generate call."""
    hub = _StubHub(responses=["not json", _bs_payload()])
    extractor = Extractor(hub)
    validator = Validator()

    extract_with_retry(
        extractor,
        validator,
        "<table></table>",
        StatementType.BALANCE_SHEET,
        retry_temperature=0.05,
    )

    assert hub.generate_calls[1]["temperature"] == pytest.approx(0.05)


def test_extract_with_retry_error_hint_in_retry_prompt() -> None:
    """The retry prompt carries the first validation error_hint."""
    hub = _StubHub(responses=["not json", _bs_payload()])
    extractor = Extractor(hub)
    validator = Validator()

    extract_with_retry(
        extractor,
        validator,
        "<table></table>",
        StatementType.BALANCE_SHEET,
    )

    retry_call = hub.generate_calls[1]
    # The build_retry_prompt template includes 「上一次输出 JSON 解析失败（{hint}）」.
    assert "上一次输出 JSON 解析失败" in retry_call["prompt"]
    # And the original prompt content is preserved (the table html).
    assert "<table></table>" in retry_call["prompt"]


# ---------------------------------------------------------------------------
# JSON Schema file integrity
# ---------------------------------------------------------------------------


def test_statement_schema_json_file_is_valid_json() -> None:
    """The statement_schema.json file on disk is valid JSON and loadable."""
    from pathlib import Path

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "schemas"
        / "statement_schema.json"
    )
    with open(schema_path, encoding="utf-8") as fp:
        schema = json.load(fp)

    assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert schema["title"] == "FinancialStatement"
    assert "report_period" in schema["properties"]
    assert "statements" in schema["properties"]


def test_statement_schema_json_matches_pydantic_model() -> None:
    """The committed JSON Schema matches FinancialStatement.model_json_schema().

    Guards against drift between the Pydantic model and the committed
    schema file. If this test fails, regenerate statement_schema.json
    per the instructions in its top-level description field.
    """
    from pathlib import Path

    from app.schemas.statement import FinancialStatement

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "schemas"
        / "statement_schema.json"
    )
    with open(schema_path, encoding="utf-8") as fp:
        committed = json.load(fp)

    pydantic_schema = FinancialStatement.model_json_schema()
    # Pydantic-generated schema lacks $schema/$id; strip the extras we added.
    # Also strip description because we rewrote it in the committed file to
    # carry regeneration instructions for humans.
    skip_keys = {"$schema", "$id", "title", "description"}
    committed_core = {k: v for k, v in committed.items() if k not in skip_keys}
    pydantic_core = {k: v for k, v in pydantic_schema.items() if k not in skip_keys}

    assert committed_core == pydantic_core
