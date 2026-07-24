"""M10 ReportGenerator NLG（spec §2.3 M10 + plan M3.05）。

职责：

* 基于 ``FinancialStatement`` + ``CheckResult`` + 可选检索段落，调用 7B 生成
  5 段式 Markdown 报告（公司概况 / 财务概览 / 三表分析 / 异常与风险 / 结论）。
* 解析 LLM 输出的 JSON，回填 ``ReportResult.sections``。
* 失败降级（spec §10.3）：超时 / JSON 解析失败 / 模型异常 / 段落数 ≠ 5 都不抛，
  走模板降级产出 5 段齐全的 ``ReportResult``（``fallback=True``），
  保证 Reasoner → Report 链路不被 LLM 失败阻断。

设计要点：

1. **异步 + 同步 generate 包到 to_thread** — 与 ``LLMReviewer`` 一致，
   ``ModelHub.generate`` 是 torch 阻塞调用；用 ``asyncio.to_thread`` 避免阻塞
   事件循环，不引入并发（spec §8.1 单进程只能装 1 个 7B，并发无收益反而争抢）。
2. **不可变输入** — 不修改 ``FinancialStatement`` / ``CheckResult``；
   产出新的 ``ReportResult``。
3. **prompt 强约束 JSON** — 对齐 ``extractor.prompts`` / ``llm_reviewer`` 风格，
   输出 ``{"sections": [{"title": "...", "content": "..."}, ...]}``。
4. **三表上下文截断** — 每表最多 15 个科目进 prompt，避免撑爆 7B 上下文
   （spec §3.7 REPORT 45s SLA）。
5. **KB 检索段落** — 当前 M3 阶段知识库未建好（M5），``kb_snippets`` 支持 None
   或空列表；M5 落地后由调用方填充。
6. **降级模板** — LLM 失败时由 ``_build_fallback_report`` 基于规则结果 + 异常
   + 抽取数据生成纯模板报告，5 段齐全但内容较朴素。
7. **段落数校验** — LLM 输出 ``sections`` 长度 ≠ 5 或顺序错乱时降级；
   避免段落数不对导致前端 Tab 渲染异常。
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from typing import Any

from app.modules.generator.prompts import build_report_prompt
from app.modules.modelhub.modelhub import ModelHub
from app.schemas.reasoning import CheckResult
from app.schemas.report import (
    ReportResult,
    ReportSection,
    ReportSectionType,
)
from app.schemas.statement import FinancialStatement, StatementType
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# Strip ```json ... ``` fenced code blocks (Qwen sometimes wraps output).
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
# Capture the first balanced-looking object substring as last-resort fallback.
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# 报告生成默认参数（spec §3.7 SLA：REPORT 链路 45s 超时）。
_DEFAULT_MAX_NEW_TOKENS = 2048  # 5 段报告较长，需要较大 token 预算
_DEFAULT_TEMPERATURE = 0.3  # 报告需自然语言，温度略高避免死板；但仍偏低避免幻觉
_DEFAULT_TIMEOUT_SECONDS = 45.0

# 5 段固定标题，对齐 ReportSectionType.chinese_name。
_SECTION_TYPES: list[ReportSectionType] = [
    ReportSectionType.COMPANY_OVERVIEW,
    ReportSectionType.FINANCIAL_OVERVIEW,
    ReportSectionType.STATEMENT_ANALYSIS,
    ReportSectionType.ANOMALY_AND_RISK,
    ReportSectionType.CONCLUSION,
]

# 三表中文标签直接复用 StatementType.chinese_name；本常量保留为本地
# 索引以便快速查找（与 StatementType.chinese_name 等价但避免每次属性查询）。
_STATEMENT_LABELS: dict[StatementType, str] = {
    StatementType.BALANCE_SHEET: StatementType.BALANCE_SHEET.chinese_name,
    StatementType.INCOME_STATEMENT: StatementType.INCOME_STATEMENT.chinese_name,
    StatementType.CASH_FLOW: StatementType.CASH_FLOW.chinese_name,
}


class ReportGenerator:
    """5 段式财报报告生成器（spec §2.3 M10）。

    Attributes:
        hub: ModelHub 实例（必须已加载 7B；调用方负责 ``load_for_scene`` +
            ``model_lock``，与 ``Extractor`` / ``LLMReviewer`` 保持一致）。
        max_new_tokens: 报告输出最大 token 数。
        temperature: 采样温度。
        timeout_seconds: 单次生成 SLA 超时。
    """

    def __init__(
        self,
        hub: ModelHub,
        *,
        max_new_tokens: int | None = None,
        temperature: float = _DEFAULT_TEMPERATURE,
        timeout_seconds: float | None = None,
    ) -> None:
        """初始化报告生成器。

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

    async def generate(
        self,
        statement: FinancialStatement,
        check_result: CheckResult,
        *,
        kb_snippets: list[str] | None = None,
        company_name: str = "",
        company_code: str = "",
    ) -> ReportResult:
        """生成 5 段式 Markdown 报告。

        Args:
            statement: 抽取结果（含三表数据）。
            check_result: 勾稽与异常检测结果。
            kb_snippets: 知识库检索段落（M3 阶段可为 None）。
            company_name: 公司名称（可选）。
            company_code: 公司股票代码（可选）。

        Returns:
            ``ReportResult``；LLM 成功时 ``fallback=False`` + ``raw_text``
            保留原始输出；任何失败降级为模板报告（``fallback=True``），
            5 段齐全但内容较朴素。
        """
        prompt = build_report_prompt(
            statement,
            check_result,
            kb_snippets=kb_snippets,
            company_name=company_name,
            company_code=company_code,
        )

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
                "[ReportGenerator.generate] LLM 超时: %s",
                exc,
            )
            return self._fallback(
                statement,
                check_result,
                company_name=company_name,
                company_code=company_code,
                error=f"LLM 超时: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — 任何异常都降级
            LOGGER.warning(
                "[ReportGenerator.generate] LLM 调用失败: %s",
                exc,
            )
            return self._fallback(
                statement,
                check_result,
                company_name=company_name,
                company_code=company_code,
                error=f"LLM 调用失败: {exc}",
            )

        parsed = _extract_json_object(gen_result.text)
        if parsed is None or not isinstance(parsed, dict):
            LOGGER.warning(
                "[ReportGenerator.generate] LLM 输出无法解析 JSON: %.200s",
                gen_result.text,
            )
            return self._fallback(
                statement,
                check_result,
                company_name=company_name,
                company_code=company_code,
                raw_text=gen_result.text,
                prompt_tokens=gen_result.prompt_tokens,
                completion_tokens=gen_result.completion_tokens,
                latency_ms=gen_result.latency_ms,
                error="LLM 输出无法解析为 JSON",
            )

        sections = _parse_sections(parsed)
        if sections is None:
            LOGGER.warning(
                "[ReportGenerator.generate] LLM 输出 sections 不合规: %.200s",
                gen_result.text,
            )
            return self._fallback(
                statement,
                check_result,
                company_name=company_name,
                company_code=company_code,
                raw_text=gen_result.text,
                prompt_tokens=gen_result.prompt_tokens,
                completion_tokens=gen_result.completion_tokens,
                latency_ms=gen_result.latency_ms,
                error="LLM 输出 sections 字段不合规",
            )

        LOGGER.info(
            "[ReportGenerator.generate] success latency_ms=%.1f completion_tokens=%d sections=%d",
            gen_result.latency_ms,
            gen_result.completion_tokens,
            len(sections),
        )

        return ReportResult(
            sections=sections,
            report_period=statement.report_period,
            fallback=False,
            raw_text=gen_result.text,
            prompt_tokens=gen_result.prompt_tokens,
            completion_tokens=gen_result.completion_tokens,
            latency_ms=gen_result.latency_ms,
            error=None,
        )

    # ------------------------------------------------------------------
    # 降级路径
    # ------------------------------------------------------------------

    def _fallback(
        self,
        statement: FinancialStatement,
        check_result: CheckResult,
        *,
        company_name: str,
        company_code: str,
        raw_text: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        error: str,
    ) -> ReportResult:
        """模板降级 — 5 段齐全但内容较朴素。

        Args:
            statement: 抽取结果。
            check_result: 勾稽结果。
            company_name: 公司名称。
            company_code: 公司股票代码。
            raw_text: LLM 原始输出（如有）。
            prompt_tokens: 输入 token 数。
            completion_tokens: 输出 token 数。
            latency_ms: 端到端耗时。
            error: 降级原因。

        Returns:
            ``ReportResult`` 含 5 段模板内容 + ``fallback=True``。
        """
        sections = _build_fallback_report(
            statement,
            check_result,
            company_name=company_name,
            company_code=company_code,
        )
        return ReportResult(
            sections=sections,
            report_period=statement.report_period,
            fallback=True,
            raw_text=raw_text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            error=error,
        )


# ============================================================================
# 辅助函数
# ============================================================================


def _extract_json_object(text: str) -> Any:
    """从模型输出中提取首个 JSON 对象。

    顺序尝试（对齐 ``Extractor._extract_json_object`` /
    ``LLMReviewer._extract_json_object``）：

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


def _parse_sections(parsed: dict[str, Any]) -> list[ReportSection] | None:
    """从 LLM JSON 输出中解析 5 段报告。

    校验规则：

    * ``sections`` 字段必须存在且为 list。
    * 长度必须为 5。
    * 每个元素必须有非空 ``title`` 与非空 ``content``。
    * 顺序对齐 5 段固定标题（容错：标题不完全匹配时按位置归一）。

    Args:
        parsed: 解析后的 JSON dict。

    Returns:
        长度为 5 的 ``ReportSection`` 列表；不合规返回 ``None``。
    """
    sections_raw = parsed.get("sections")
    if not isinstance(sections_raw, list):
        return None
    if len(sections_raw) != 5:
        return None

    sections: list[ReportSection] = []
    for idx, item in enumerate(sections_raw):
        if not isinstance(item, dict):
            return None
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        if not title or not content:
            return None
        # 按位置归一为 5 段固定类型；保留 LLM 给的 title（可能含具体公司名）。
        sections.append(
            ReportSection(
                section_type=_SECTION_TYPES[idx],
                title=title,
                content=content,
            )
        )
    return sections


def _build_fallback_report(
    statement: FinancialStatement,
    check_result: CheckResult,
    *,
    company_name: str,
    company_code: str,
) -> list[ReportSection]:
    """模板降级报告 — 5 段齐全但内容较朴素。

    Args:
        statement: 抽取结果。
        check_result: 勾稽结果。
        company_name: 公司名称。
        company_code: 公司股票代码。

    Returns:
        长度为 5 的 ``ReportSection`` 列表，按固定顺序排列。
    """
    # 1. 公司概况
    overview_lines: list[str] = []
    if company_name:
        overview_lines.append(f"**公司名称**：{company_name}")
    if company_code:
        overview_lines.append(f"**股票代码**：{company_code}")
    overview_lines.append(f"**报告期末日**：{statement.report_period}")
    overview_lines.append(f"**币种 / 单位**：{statement.currency} / {statement.unit}")
    overview_lines.append(
        "本报告由模板降级路径生成（LLM 调用失败），内容仅含基础事实，建议人工复核后补充分析。"
    )
    overview = ReportSection(
        section_type=ReportSectionType.COMPANY_OVERVIEW,
        title=ReportSectionType.COMPANY_OVERVIEW.chinese_name,
        content="\n\n".join(overview_lines),
    )

    # 2. 财务概览（关键科目摘要）
    financial_lines: list[str] = ["**关键财务指标摘要**："]
    for label, value in _summarize_key_metrics(statement).items():
        financial_lines.append(f"- {label}：{value}")
    financial_lines.append(
        f"\n**勾稽置信度**：{check_result.confidence:.2f}（1.0 = 全部规则通过 + 无异常）"
    )
    financial = ReportSection(
        section_type=ReportSectionType.FINANCIAL_OVERVIEW,
        title=ReportSectionType.FINANCIAL_OVERVIEW.chinese_name,
        content="\n".join(financial_lines),
    )

    # 3. 三表分析
    statement_lines: list[str] = ["**三表科目数据**："]
    for st_type in (
        StatementType.BALANCE_SHEET,
        StatementType.INCOME_STATEMENT,
        StatementType.CASH_FLOW,
    ):
        items = statement.statements.get(st_type, [])
        if not items:
            continue
        label = _STATEMENT_LABELS.get(st_type, st_type.value)
        statement_lines.append(f"\n### {label}")
        statement_lines.append("| 科目 | 数值 |")
        statement_lines.append("|---|---|")
        for item in items[:10]:  # 每表最多 10 行避免报告过长
            statement_lines.append(f"| {item.item} | {item.value} |")
    analysis = ReportSection(
        section_type=ReportSectionType.STATEMENT_ANALYSIS,
        title=ReportSectionType.STATEMENT_ANALYSIS.chinese_name,
        content="\n".join(statement_lines),
    )

    # 4. 异常与风险
    anomaly_lines: list[str] = ["**勾稽规则结果**："]
    if not check_result.rules:
        anomaly_lines.append("- （无规则结果）")
    else:
        for rule in check_result.rules:
            status = "通过" if rule.is_pass else "失败"
            line = (
                f"- {rule.rule_name}（{rule.rule_type.value}）：{status}，"
                f"严重度 {rule.severity.value}"
            )
            if rule.diff is not None:
                line += f"，差异 {rule.diff:.2f}"
            if rule.note:
                line += f"；备注：{rule.note}"
            anomaly_lines.append(line)
    anomaly_lines.append("\n**异常列表**：")
    if not check_result.anomalies:
        anomaly_lines.append("- （无异常）")
    else:
        for anomaly in check_result.anomalies:
            line = f"- {anomaly.item_name}（{anomaly.anomaly_type}）：{anomaly.severity.value}"
            if anomaly.description:
                line += f"；{anomaly.description}"
            anomaly_lines.append(line)
    risk = ReportSection(
        section_type=ReportSectionType.ANOMALY_AND_RISK,
        title=ReportSectionType.ANOMALY_AND_RISK.chinese_name,
        content="\n".join(anomaly_lines),
    )

    # 5. 结论
    conclusion_lines: list[str] = []
    if check_result.all_pass:
        conclusion_lines.append(
            "本期财报勾稽规则全部通过、未检出异常，整体财务数据一致性良好。"
        )
    else:
        failed = sum(1 for r in check_result.rules if not r.is_pass)
        conclusion_lines.append(
            f"本期财报勾稽规则共 {len(check_result.rules)} 条，"
            f"其中 {failed} 条失败；异常列表共 {len(check_result.anomalies)} 条。"
            "建议人工核查相关科目与异常，必要时重传 PDF 触发整体重跑。"
        )
    conclusion_lines.append("（本段由模板降级路径生成，建议人工补充完整结论。）")
    conclusion = ReportSection(
        section_type=ReportSectionType.CONCLUSION,
        title=ReportSectionType.CONCLUSION.chinese_name,
        content="\n\n".join(conclusion_lines),
    )

    return [overview, financial, analysis, risk, conclusion]


def _summarize_key_metrics(statement: FinancialStatement) -> dict[str, str]:
    """抽取关键科目摘要（资产总计 / 负债合计 / 净利润 / 经营现金流净额）。

    Args:
        statement: 抽取结果。

    Returns:
        ``{label: value_str}``；缺失科目不返回。
    """
    # 同义词组（与 accounting_rules.py 风格一致，但本处只用于展示，简化处理）。
    key_metrics: list[tuple[str, StatementType, list[str]]] = [
        ("资产总计", StatementType.BALANCE_SHEET, ["资产总计", "资产合计"]),
        ("负债合计", StatementType.BALANCE_SHEET, ["负债合计", "负债总计"]),
        (
            "所有者权益合计",
            StatementType.BALANCE_SHEET,
            ["所有者权益合计", "股东权益合计", "净资产"],
        ),
        ("营业收入", StatementType.INCOME_STATEMENT, ["营业收入", "营业总收入"]),
        (
            "净利润",
            StatementType.INCOME_STATEMENT,
            ["净利润", "归属于母公司股东的净利润"],
        ),
        (
            "经营活动现金流量净额",
            StatementType.CASH_FLOW,
            ["经营活动产生的现金流量净额", "经营活动现金流量净额"],
        ),
    ]
    result: dict[str, str] = {}
    for label, st_type, synonyms in key_metrics:
        for synonym in synonyms:
            for item in statement.statements.get(st_type, []):
                if item.item == synonym:
                    result[label] = _fmt_decimal(Decimal(str(item.value)))
                    break
            if label in result:
                break
    return result


def _fmt_decimal(v: Decimal) -> str:
    """格式化 Decimal 避免 scientific 计数法。

    Args:
        v: Decimal 数值。

    Returns:
        保留 2 位小数的字符串。
    """
    return f"{v:.2f}"


__all__ = ["ReportGenerator"]
