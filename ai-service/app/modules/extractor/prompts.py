"""M7 extraction prompts for the 7B general-prompt baseline (spec §2.3 M7).

``build_extract_prompt`` renders a Qwen2.5-Instruct chat-style prompt
that strongly constrains the model to emit a JSON object matching the
``FinancialStatement`` schema. The prompt is intentionally minimal —
T1 fine-tuned 1.5B QLoRA (M4) will replace it with a shorter task
prompt, but the JSON contract stays identical.

Key design choices:

* **System role** declares the model as a financial-report extraction
  assistant and forbids any prose outside JSON.
* **User role** carries: statement type + Chinese label, report period,
  company code (optional), unit hint, and the raw HTML/Markdown table.
* **Output format** is fixed: ``{report_period, currency, unit,
  statements: {<type>: [{item, value, scope, period}, ...]}}``.
* **Few-shot** is omitted on purpose — the 7B baseline relies on the
  schema description alone; few-shot examples bloat the prompt and
  inflate token cost on a 6GB-VRAM box.
"""

from __future__ import annotations

from app.schemas.statement import StatementType

_SYSTEM_PROMPT = (
    "你是一名专业的A股财报分析助手。任务是从给定的财务报表 HTML 中抽取结构化"
    "科目数据，严格输出 JSON 对象，禁止输出任何额外文字、Markdown 代码块或解释。"
    "数值字段必须是数字（不要带千分位逗号、单位或括号）；负数用负号表示。"
    "scope 字段只能是「合并」或「母公司」；period 字段只能是「本期」「上期」"
    "「本年累计」「上年同期」。"
)

_OUTPUT_SCHEMA_HINT = """{
  "report_period": "YYYY-MM-DD",
  "currency": "CNY",
  "unit": "元",
  "statements": {
    "<statement_type>": [
      {"item": "科目名称", "value": 1234567890.00, "scope": "合并", "period": "本期"}
    ]
  }
}"""


def build_extract_prompt(
    table_html: str,
    statement_type: StatementType,
    *,
    report_period: str = "",
    company_code: str = "",
    unit: str = "元",
) -> str:
    """Render the chat-style extraction prompt for one statement table.

    Args:
        table_html: Raw HTML or Markdown table markup from the parser.
            Empty input is rejected by ``Extractor`` before this function
            is called; here we just forward it.
        statement_type: Which statement to extract (BS / IS / CF).
        report_period: Report end date ``YYYY-MM-DD``; empty string when
            unknown (model is told to fill ``report_period`` itself).
        company_code: A-share ticker (e.g. ``"600519"``); optional.
        unit: Unit hint for the value field (``元`` / ``万元`` / ``百万元``).

    Returns:
        A single prompt string ready for ``ModelHub.generate``. Caller
        wraps it into a chat template if the backend requires it.
    """
    header_lines = [
        f"请从下面的「{statement_type.chinese_name}」HTML 表格中抽取所有科目数据。",
        f'目标 statement_type = "{statement_type.value}"。',
    ]
    if report_period:
        header_lines.append(f"报告期末日：{report_period}")
    if company_code:
        header_lines.append(f"公司股票代码：{company_code}")
    if unit:
        header_lines.append(
            f'数值单位：{unit}（保持与原表一致；如原表为「万元」请填 unit="万元" 且 value 用万元单位）'
        )

    header = "\n".join(header_lines)
    schema_block = (
        "输出格式（严格 JSON，键名固定，不要省略 statements 字段）：\n"
        f"{_OUTPUT_SCHEMA_HINT.replace('<statement_type>', statement_type.value)}"
    )
    return (
        f"<|im_start|>system\n{_SYSTEM_PROMPT}<|im_end|>\n"
        f"<|im_start|>user\n{header}\n\n{schema_block}\n\n"
        f"表格内容：\n{table_html}\n"
        f"<|im_end|>\n<|im_start|>assistant\n"
    )


def build_retry_prompt(
    original_prompt: str,
    *,
    error_hint: str,
) -> str:
    """Append a low-temperature retry directive to a failed prompt.

    Used by the M2.07 validator when the first extraction returns
    malformed JSON. The retry nudge keeps temperature low (set by the
    caller via ``GenerateRequest.temperature``) and references the
    parse error so the model can self-correct.

    Args:
        original_prompt: The prompt that produced the bad output.
        error_hint: Short parse error string (e.g. ``"Expecting ','"``).

    Returns:
        A new prompt string carrying the original content + retry nudge.
    """
    return (
        f"{original_prompt}"
        f"注意：上一次输出 JSON 解析失败（{error_hint}）。"
        "请只输出严格合法的 JSON 对象，不要包含注释、Markdown 代码块或多余文字。\n"
    )


__all__ = [
    "build_extract_prompt",
    "build_retry_prompt",
]
