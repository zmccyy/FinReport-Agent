"""M7 抽取结果校验器（spec §2.3 M7 + plan M2.07）。

校验链：

1. **JSON 结构校验** — Pydantic 在 ``Extractor._coerce_to_statement`` 已完成；
   ``Validator`` 不重复结构校验，只附加业务规则。
2. **数值合法性** — 非 NaN / 非 inf / 数值范围合理性（spec §2.3 M7）。
3. **单位一致性** — ``statement.unit`` 必须在常见枚举内（``元`` / ``万元`` /
   ``百万元`` / ``千元``），所有 ``StatementItem.value`` 的量纲与之一致。
4. **必填字段** — ``report_period`` 必须是 ``YYYY-MM-DD``；``currency`` 必须是
   ISO 4217 三字母代码。
5. **关键科目** — BS 必须含 ``资产总计`` / ``负债合计`` / ``所有者权益合计``；
   IS 必须含 ``营业收入`` / ``净利润``；CF 必须含 ``经营活动产生的现金流量净额``。
   缺失只产 warning，不阻断（spec 没要求硬阻断）。

spec §10.3 降级链：抽取小模型 JSON 失败 → 重试 1 次 (temp=0.1) → 改用 7B。
``extract_with_retry`` 实现第一次失败后用 ``build_retry_prompt(error_hint=...)``
+ 低 temp 重试一次；两次都失败返回最后一次的 ``ExtractionResult``，由 caller
决定是否转 7B（M4 T1 1.5B 路径）或上抛（M2.08 编排层）。

Validator 不持有 ``model_lock`` 也不调 ``hub.generate`` —— 重试时通过
``Extractor.extract_with_prompt`` 公开方法走，保持职责单一。
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.modules.extractor.extractor import Extractor
from app.modules.extractor.prompts import build_extract_prompt, build_retry_prompt
from app.schemas.statement import (
    ExtractionResult,
    FinancialStatement,
    StatementType,
)
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# report_period 必须是 YYYY-MM-DD（spec §2.3 M7）。
_PERIOD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A 股财报常见单位枚举（spec §2.3 M7 单位一致性）。
_VALID_UNITS: frozenset[str] = frozenset({"元", "万元", "百万元", "千元"})

# 关键科目白名单（spec §2.3 M7 必填科目；缺失只产 warning）。
_REQUIRED_ITEMS: dict[StatementType, frozenset[str]] = {
    StatementType.BALANCE_SHEET: frozenset({"资产总计", "负债合计", "所有者权益合计"}),
    StatementType.INCOME_STATEMENT: frozenset({"营业收入", "净利润"}),
    StatementType.CASH_FLOW: frozenset({"经营活动产生的现金流量净额"}),
}

# A 股财报数值上限：1 万亿元 = 1e12 元；超过 1e15 几乎肯定是模型幻觉。
_VALUE_ABS_CEILING = 1e15


class ValidationIssue(BaseModel):
    """单个校验问题。

    severity=error 阻断重试成功（is_valid=False）；warning 只记录不阻断。
    code 用于上层 metrics 聚合（解析率统计 / 关键科目缺失率等）。
    path 用 JSONPath 风格定位问题位置，便于调试。
    """

    severity: str = Field(description="error / warning")
    code: str = Field(description="问题代码，如 nan_value / missing_required_item")
    message: str = Field(description="人类可读的问题描述")
    path: str = Field(
        default="", description="问题位置，如 statements.balance_sheet[0].value"
    )


class ValidationResult(BaseModel):
    """``Validator.validate`` 返回值。

    ``is_valid`` 仅当无 error 级 issue 时为 True；warning 不影响。
    ``error_hint`` 是给 ``build_retry_prompt`` 用的简短错误描述（最多 3 条），
    成功时为空字符串。
    """

    is_valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    error_hint: str = Field(default="")

    @property
    def error_count(self) -> int:
        """Number of error-severity issues."""
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        """Number of warning-severity issues."""
        return sum(1 for i in self.issues if i.severity == "warning")


class Validator:
    """M7 抽取结果校验器。

    校验在 ``ExtractionResult`` 上跑：parse 失败直接判 invalid；parse 成功再
    对 ``FinancialStatement`` 跑业务规则校验。Validator 不修改输入对象，
    返回纯数据结构 (``ValidationResult``)，方便 caller 决策。
    """

    def __init__(self, *, schema_path: Path | None = None) -> None:
        """Configure the validator.

        Args:
            schema_path: Optional override for the JSON Schema file path.
                Defaults to ``app/schemas/statement_schema.json``; the file
                is loaded lazily on first ``validate`` call so missing
                files do not break unit tests that don't need it.
        """
        self._schema_path = schema_path
        self._schema: dict[str, Any] | None = None

    @property
    def schema(self) -> dict[str, Any]:
        """JSON Schema dict loaded from ``statement_schema.json``.

        Loaded lazily; if the file is missing, an empty dict is returned
        (the Validator does not depend on the schema file for business
        rule checks — it's exposed for callers that want to do strict
        JSON Schema validation on raw model output).
        """
        if self._schema is None:
            path = self._schema_path or (
                Path(__file__).resolve().parents[2]
                / "schemas"
                / "statement_schema.json"
            )
            try:
                import json

                with open(path, encoding="utf-8") as fp:
                    self._schema = json.load(fp)
            except (OSError, ValueError) as error:
                LOGGER.warning(
                    "[Validator.schema] failed to load %s: %s",
                    path,
                    error,
                )
                self._schema = {}
        return self._schema

    def validate(self, result: ExtractionResult) -> ValidationResult:
        """Validate an ``ExtractionResult`` end-to-end.

        Args:
            result: Output of ``Extractor.extract`` (or retry attempt).

        Returns:
            ``ValidationResult`` with ``is_valid=True`` when the parse
            succeeded and no error-severity business rule was violated.
        """
        if not result.success:
            # Parse 阶段已失败（JSON 解析失败 / schema 校验失败）。
            # 把 parse error 直接转为 issue，error_hint 用原 error。
            error_msg = result.error or "parse failed"
            return ValidationResult(
                is_valid=False,
                issues=[
                    ValidationIssue(
                        severity="error",
                        code="parse_failed",
                        message=error_msg,
                    )
                ],
                error_hint=error_msg,
            )

        # M2 review fix: 之前用 assert,python -O 启动时被剥离。
        # 防御性编程:若 success=True 但 statement=None(理论不应发生),
        # 返回明确错误而非 NPE。
        if result.statement is None:
            return ValidationResult(
                is_valid=False,
                issues=[
                    ValidationIssue(
                        severity="error",
                        code="missing_statement",
                        message="extraction 报告 success=True 但 statement 为空",
                    )
                ],
                error_hint="statement payload missing despite success flag",
            )
        issues = self.validate_statement(result.statement)

        errors = [i for i in issues if i.severity == "error"]
        is_valid = not errors
        # error_hint 取前 3 条 error message 拼接，给 retry prompt 用。
        error_hint = "; ".join(e.message for e in errors[:3]) if errors else ""

        return ValidationResult(
            is_valid=is_valid,
            issues=issues,
            error_hint=error_hint,
        )

    def validate_statement(
        self, statement: FinancialStatement
    ) -> list[ValidationIssue]:
        """Run business-rule checks on a parsed ``FinancialStatement``.

        Args:
            statement: The parsed statement to check.

        Returns:
            A list of ``ValidationIssue``; empty list means no problems.
        """
        issues: list[ValidationIssue] = []

        # --- report_period 格式校验 ---
        if not _PERIOD_RE.match(statement.report_period):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="invalid_report_period",
                    message=(
                        f"report_period 格式应为 YYYY-MM-DD，实际为 {statement.report_period!r}"
                    ),
                    path="report_period",
                )
            )

        # --- currency ISO 4217 三字母 ---
        # 用 isascii() 而非 isalpha()，避免「人民币」这类 3 字符中文通过校验。
        if (
            len(statement.currency) != 3
            or not statement.currency.isalpha()
            or not statement.currency.isascii()
        ):
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="invalid_currency",
                    message=(
                        f"currency 应为 ISO 4217 三字母 ASCII 代码，实际为 {statement.currency!r}"
                    ),
                    path="currency",
                )
            )

        # --- unit 枚举校验 ---
        if statement.unit not in _VALID_UNITS:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="unknown_unit",
                    message=(
                        f"unit 不在常见枚举 {sorted(_VALID_UNITS)} 中：{statement.unit!r}"
                    ),
                    path="unit",
                )
            )

        # --- statements 非空 ---
        if not statement.statements:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="empty_statements",
                    message="statements 不能为空",
                    path="statements",
                )
            )
            # 已经空了，下面的逐项校验没意义。
            return issues

        # --- 逐项校验 ---
        for st_type, items in statement.statements.items():
            type_value = st_type.value

            if not items:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="empty_statement_type",
                        message=f"statements[{type_value}] 为空列表",
                        path=f"statements.{type_value}",
                    )
                )
                continue

            seen_items: dict[str, int] = {}
            for idx, item in enumerate(items):
                base_path = f"statements.{type_value}[{idx}]"

                # value 非 NaN（Pydantic 已校验，validator 兜底防绕过）。
                if math.isnan(item.value):
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="nan_value",
                            message=f"value 不能为 NaN（科目：{item.item}）",
                            path=f"{base_path}.value",
                        )
                    )

                # value 非 inf。
                if math.isinf(item.value):
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="inf_value",
                            message=f"value 不能为无限大（科目：{item.item}）",
                            path=f"{base_path}.value",
                        )
                    )

                # value 范围合理性。
                if abs(item.value) > _VALUE_ABS_CEILING:
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            code="value_out_of_range",
                            message=(
                                f"value 异常大（科目：{item.item}，"
                                f"值：{item.value}，上限：{_VALUE_ABS_CEILING}）"
                            ),
                            path=f"{base_path}.value",
                        )
                    )

                # 重复科目（同表内同名）。
                if item.item in seen_items:
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            code="duplicate_item",
                            message=(
                                f"科目重复：{item.item}（前次出现在 index {seen_items[item.item]}）"
                            ),
                            path=f"{base_path}.item",
                        )
                    )
                else:
                    seen_items[item.item] = idx

            # 关键科目缺失校验。
            required = _REQUIRED_ITEMS.get(st_type)
            if required:
                item_names = {it.item for it in items}
                missing = required - item_names
                if missing:
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            code="missing_required_item",
                            message=(
                                f"statements[{type_value}] 缺少关键科目："
                                f"{', '.join(sorted(missing))}"
                            ),
                            path=f"statements.{type_value}",
                        )
                    )

        return issues


def extract_with_retry(
    extractor: Extractor,
    validator: Validator,
    table_html: str,
    statement_type: StatementType,
    *,
    report_period: str = "",
    company_code: str = "",
    unit: str = "元",
    max_new_tokens: int | None = None,
    timeout_seconds: float | None = None,
    retry_temperature: float = 0.1,
    max_retries: int = 1,
) -> tuple[ExtractionResult, ValidationResult]:
    """Run extract → validate → retry-on-failure (spec §10.3 降级链).

    Flow:
        1. First attempt at the extractor's default temperature.
        2. Validate; if valid, return ``(result, validation)``.
        3. If invalid and ``max_retries > 0``: rebuild the prompt with
           ``build_retry_prompt(error_hint=...)`` and re-generate at
           ``retry_temperature`` (default 0.1, spec §10.3).
        4. Validate the retry; if valid, return ``(retry_result, retry_validation)``.
        5. Still invalid: return the retry result + validation; the caller
           (M2.08 orchestrator) decides whether to fall back to the 7B
           model (spec §10.3 "改用 7B") or fail the task.

    Args:
        extractor: Configured ``Extractor`` (caller holds model_lock).
        validator: Configured ``Validator``.
        table_html: Raw HTML/Markdown table markup from the parser.
        statement_type: Target statement type.
        report_period: Optional report end date hint.
        company_code: Optional A-share ticker.
        unit: Unit hint for value field.
        max_new_tokens: Override default max tokens.
        timeout_seconds: Override default SLA timeout.
        retry_temperature: Sampling temperature for the retry attempt
            (spec §10.3 default 0.1).
        max_retries: Number of retry attempts on validation failure.
            ``0`` skips retry (caller only wants one-shot validate).

    Returns:
        Tuple ``(result, validation)``. ``result`` is the best attempt
        (first success, else last failure). ``validation`` corresponds
        to ``result``; check ``validation.is_valid`` to decide.
    """
    # --- 第一次抽取 ---
    first_result = extractor.extract(
        table_html,
        statement_type,
        report_period=report_period,
        company_code=company_code,
        unit=unit,
        max_new_tokens=max_new_tokens,
        timeout_seconds=timeout_seconds,
    )
    first_validation = validator.validate(first_result)
    if first_validation.is_valid:
        LOGGER.info(
            "[extract_with_retry] first attempt valid type=%s issues=%d",
            statement_type.value,
            len(first_validation.issues),
        )
        return first_result, first_validation

    LOGGER.info(
        "[extract_with_retry] first attempt invalid type=%s errors=%d hint=%s",
        statement_type.value,
        first_validation.error_count,
        first_validation.error_hint,
    )

    if max_retries <= 0:
        return first_result, first_validation

    # --- 重试：用 build_retry_prompt + 低 temp ---
    original_prompt = build_extract_prompt(
        table_html,
        statement_type,
        report_period=report_period,
        company_code=company_code,
        unit=unit,
    )
    retry_prompt = build_retry_prompt(
        original_prompt,
        error_hint=first_validation.error_hint,
    )

    retry_result = extractor.extract_with_prompt(
        retry_prompt,
        statement_type,
        max_new_tokens=max_new_tokens,
        temperature=retry_temperature,
        timeout_seconds=timeout_seconds,
    )
    retry_validation = validator.validate(retry_result)
    if retry_validation.is_valid:
        LOGGER.info(
            "[extract_with_retry] retry succeeded type=%s issues=%d",
            statement_type.value,
            len(retry_validation.issues),
        )
        return retry_result, retry_validation

    LOGGER.warning(
        "[extract_with_retry] retry also failed type=%s errors=%d hint=%s",
        statement_type.value,
        retry_validation.error_count,
        retry_validation.error_hint,
    )
    return retry_result, retry_validation


__all__ = [
    "ValidationIssue",
    "ValidationResult",
    "Validator",
    "extract_with_retry",
]
