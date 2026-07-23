"""M10 报告生成 prompts（spec §2.3 M10 + plan M3.05）。

``build_report_prompt`` 渲染 chat-style prompt（对齐 ``extractor.prompts`` /
``reasoner.llm_reviewer`` 风格），强约束 7B 输出 5 段式报告 JSON。

5 段固定结构（spec §2.3 M10）：

1. 公司概况
2. 财务概览
3. 三表分析
4. 异常与风险
5. 结论

设计要点：

* **System role** 声明模型为 A 股财报分析专家，禁止输出 JSON 之外的任何文字。
* **User role** 携带：报告期末日、币种、单位、三表前若干科目、勾稽规则
  结果摘要、异常列表、可选检索段落。
* **Output format** 固定：``{"sections": [{"title": "...", "content": "..."}]}``，
  ``sections`` 长度必须为 5，顺序对齐 5 段固定标题。
* **三表上下文截断** — 每表最多 15 个科目，避免 prompt 过长撑爆 7B 上下文
  （spec §3.7 REPORT 45s SLA）。
* **Few-shot 省略** — 与 ``extractor.prompts`` 一致，依赖 schema 描述，
  few-shot 会撑爆 6GB VRAM 上的 7B 上下文。
"""

from __future__ import annotations

from decimal import Decimal

from app.schemas.reasoning import CheckResult
from app.schemas.report import ReportSectionType
from app.schemas.statement import FinancialStatement, StatementType


_SYSTEM_PROMPT = (
    "你是一名专业的A股财报分析师。任务是基于给定的财报数据、勾稽结果、"
    "异常列表与检索段落，生成一份 5 段式 Markdown 财报分析报告。"
    "严格输出 JSON 对象，禁止输出任何额外文字、Markdown 代码块或解释。"
    "sections 数组必须按顺序包含 5 段：公司概况、财务概览、三表分析、"
    "异常与风险、结论；每段的 content 字段用 Markdown 正文撰写，"
    "必须引用真实数据（数值、科目名、规则结果、异常描述），不要编造。"
    "content 不要嵌套 JSON、不要使用代码块；可以适当使用列表与加粗。"
)

_OUTPUT_SCHEMA_HINT = """{
  "sections": [
    {"title": "公司概况", "content": "Markdown 正文，约 200-400 字"},
    {"title": "财务概览", "content": "Markdown 正文，约 200-400 字"},
    {"title": "三表分析", "content": "Markdown 正文，约 300-500 字"},
    {"title": "异常与风险", "content": "Markdown 正文，约 200-400 字"},
    {"title": "结论", "content": "Markdown 正文，约 100-200 字"}
  ]
}"""

# 5 段固定标题，对齐 ReportSectionType.chinese_name。
_SECTION_TITLES: list[str] = [
    ReportSectionType.COMPANY_OVERVIEW.chinese_name,
    ReportSectionType.FINANCIAL_OVERVIEW.chinese_name,
    ReportSectionType.STATEMENT_ANALYSIS.chinese_name,
    ReportSectionType.ANOMALY_AND_RISK.chinese_name,
    ReportSectionType.CONCLUSION.chinese_name,
]

# 三表中文标签直接复用 StatementType.chinese_name；本常量保留为本地
# 索引以便快速查找（与 StatementType.chinese_name 等价但避免每次属性查询）。
_STATEMENT_LABELS: dict[StatementType, str] = {
    StatementType.BALANCE_SHEET: StatementType.BALANCE_SHEET.chinese_name,
    StatementType.INCOME_STATEMENT: StatementType.INCOME_STATEMENT.chinese_name,
    StatementType.CASH_FLOW: StatementType.CASH_FLOW.chinese_name,
}

# 三表上下文每表最多保留的科目数，避免 prompt 过长撑爆 7B 上下文。
_MAX_STATEMENT_ITEMS_PER_TABLE = 15

# 检索段落最多保留的条数，避免 prompt 过长。
_MAX_KB_SNIPPETS = 5


def build_report_prompt(
    statement: FinancialStatement,
    check_result: CheckResult,
    *,
    kb_snippets: list[str] | None = None,
    company_name: str = "",
    company_code: str = "",
) -> str:
    """构建 5 段式报告生成 prompt（chat-style，对齐 extractor / llm_reviewer 风格）。

    Args:
        statement: 抽取结果（含三表数据）。
        check_result: 勾稽与异常检测结果（含 rules + anomalies + confidence）。
        kb_snippets: 知识库检索段落列表（M5 落地后填充；M3.05 可为 None 或空）。
        company_name: 公司名称（可选）。
        company_code: 公司股票代码（可选）。

    Returns:
        单个 prompt 字符串，ready for ``ModelHub.generate``。
    """
    header_lines: list[str] = []
    if company_name:
        header_lines.append(f"公司名称：{company_name}")
    if company_code:
        header_lines.append(f"公司股票代码：{company_code}")
    header_lines.extend(
        [
            f"报告期末日：{statement.report_period}",
            f"币种：{statement.currency}",
            f"数值单位：{statement.unit}",
            f"勾稽置信度：{check_result.confidence:.2f}",
        ]
    )
    header = "\n".join(header_lines)

    # 三表上下文（每表前 N 个科目）。
    context_lines: list[str] = []
    for st_type in (
        StatementType.BALANCE_SHEET,
        StatementType.INCOME_STATEMENT,
        StatementType.CASH_FLOW,
    ):
        items = statement.statements.get(st_type, [])
        if not items:
            continue
        label = _STATEMENT_LABELS.get(st_type, st_type.value)
        sample = items[:_MAX_STATEMENT_ITEMS_PER_TABLE]
        formatted = ", ".join(
            f"{item.item}={_fmt_decimal(Decimal(str(item.value)))}" for item in sample
        )
        context_lines.append(f"  {label}: {formatted}")
    context_block = "\n".join(context_lines) if context_lines else "  (无科目数据)"

    # 勾稽规则结果摘要。
    rules_block = _format_rules(check_result)

    # 异常列表摘要。
    anomalies_block = _format_anomalies(check_result)

    # 检索段落（可选）。
    snippets_block = _format_snippets(kb_snippets)

    schema_block = (
        "输出格式（严格 JSON，键名固定，sections 数组必须按顺序包含 5 段）：\n"
        f"{_OUTPUT_SCHEMA_HINT}"
    )

    section_hint = "5 段固定标题（按顺序）：\n" + "\n".join(
        f"  {i + 1}. {title}" for i, title in enumerate(_SECTION_TITLES)
    )

    return (
        f"<|im_start|>system\n{_SYSTEM_PROMPT}<|im_end|>\n"
        f"<|im_start|>user\n{header}\n\n"
        f"三表科目数据：\n{context_block}\n\n"
        f"勾稽规则结果：\n{rules_block}\n\n"
        f"异常列表：\n{anomalies_block}\n\n"
        f"{snippets_block}\n"
        f"{section_hint}\n\n"
        f"{schema_block}\n"
        f"<|im_end|>\n<|im_start|>assistant\n"
    )


def _fmt_decimal(v: Decimal) -> str:
    """格式化 Decimal 避免 scientific 计数法。

    Args:
        v: Decimal 数值。

    Returns:
        保留 2 位小数的字符串。
    """
    return f"{v:.2f}"


def _format_rules(check_result: CheckResult) -> str:
    """格式化勾稽规则结果摘要。

    Args:
        check_result: 勾稽结果。

    Returns:
        多行文本；无规则时返回 ``(无规则结果)``。
    """
    if not check_result.rules:
        return "(无规则结果)"
    lines: list[str] = []
    for rule in check_result.rules:
        status = "通过" if rule.is_pass else "失败"
        lines.append(
            f"  - {rule.rule_name}（{rule.rule_type.value}）：{status}，"
            f"严重度 {rule.severity.value}"
        )
        if rule.diff is not None:
            lines.append(f"    差异：{_fmt_decimal(rule.diff)}")
        if rule.note:
            lines.append(f"    备注：{rule.note}")
    return "\n".join(lines)


def _format_anomalies(check_result: CheckResult) -> str:
    """格式化异常列表摘要。

    Args:
        check_result: 勾稽结果。

    Returns:
        多行文本；无异常时返回 ``(无异常)``。
    """
    if not check_result.anomalies:
        return "(无异常)"
    lines: list[str] = []
    for anomaly in check_result.anomalies:
        lines.append(f"  - {anomaly.item_name}（{anomaly.anomaly_type}）：{anomaly.severity.value}")
        if anomaly.description:
            lines.append(f"    描述：{anomaly.description}")
    return "\n".join(lines)


def _format_snippets(kb_snippets: list[str] | None) -> str:
    """格式化检索段落（可选）。

    Args:
        kb_snippets: 检索段落列表；None 或空列表返回 ``(无检索段落)``。

    Returns:
        多行文本，最多保留 ``_MAX_KB_SNIPPETS`` 条。
    """
    if not kb_snippets:
        return "检索段落：\n(无检索段落)"
    sample = kb_snippets[:_MAX_KB_SNIPPETS]
    lines = ["检索段落："]
    for idx, snippet in enumerate(sample, start=1):
        # 截断过长段落，避免撑爆 prompt。
        truncated = snippet.strip()
        if len(truncated) > 500:
            truncated = truncated[:500] + "..."
        lines.append(f"  [{idx}] {truncated}")
    return "\n".join(lines)


__all__ = ["build_report_prompt"]
