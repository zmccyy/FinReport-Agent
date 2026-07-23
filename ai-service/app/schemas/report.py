"""M10 报告生成 schemas（spec §2.3 M10 + plan M3.05）。

定义 ReportGenerator 的输入 / 输出契约：

* ``ReportSection`` — 5 段式报告中的单段（标题 + 正文）。
* ``ReportResult`` — ReportGenerator 整体输出，含 5 段 + 元数据。

设计要点：

1. **5 段固定结构** — 对齐 spec §2.3 M10：「公司概况 / 财务概览 / 三表分析
   / 异常与风险 / 结论」。``ReportSection.title`` 默认对齐这 5 段中文名。
2. **Markdown 正文** — ``content`` 是 Markdown 文本（M3.07 WeasyPrint 把
   Markdown + 图表转 PDF），支持列表 / 表格 / 加粗等基础语法。
3. **降级标记** — ``fallback`` 字段标识 LLM 失败时是否走模板降级；True 表示
   报告由硬编码模板生成，内容较朴素但仍 5 段齐全。
4. **token 统计** — ``prompt_tokens`` / ``completion_tokens`` / ``latency_ms``
   透传 LLM 调用指标，便于 M3.10 SLA 度量。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ReportSectionType(str, Enum):
    """5 段式报告段落类型（spec §2.3 M10）。

    固定顺序：公司概况 → 财务概览 → 三表分析 → 异常与风险 → 结论。
    """

    COMPANY_OVERVIEW = "company_overview"
    """公司概况"""

    FINANCIAL_OVERVIEW = "financial_overview"
    """财务概览"""

    STATEMENT_ANALYSIS = "statement_analysis"
    """三表分析"""

    ANOMALY_AND_RISK = "anomaly_and_risk"
    """异常与风险"""

    CONCLUSION = "conclusion"
    """结论"""

    @property
    def chinese_name(self) -> str:
        """段落中文名（默认 ``ReportSection.title``）。"""
        labels = {
            ReportSectionType.COMPANY_OVERVIEW: "公司概况",
            ReportSectionType.FINANCIAL_OVERVIEW: "财务概览",
            ReportSectionType.STATEMENT_ANALYSIS: "三表分析",
            ReportSectionType.ANOMALY_AND_RISK: "异常与风险",
            ReportSectionType.CONCLUSION: "结论",
        }
        return labels[self]


class ReportSection(BaseModel):
    """5 段式报告中的单段。

    Attributes:
        section_type: 段落类型枚举。
        title: 段落标题（默认对齐中文名）。
        content: Markdown 正文；M3.05 由 LLM 生成，降级时由模板填充。
    """

    section_type: ReportSectionType
    title: str = Field(description="段落标题，默认对齐 ReportSectionType.chinese_name")
    content: str = Field(default="", description="Markdown 正文")


class ReportResult(BaseModel):
    """ReportGenerator 整体输出（spec §2.3 M10）。

    Attributes:
        sections: 5 段报告（顺序固定）。
        report_period: 报告期末日 YYYY-MM-DD。
        fallback: LLM 失败时是否走模板降级；True 表示内容由硬编码模板生成。
        raw_text: LLM 原始输出（调试用，降级时为空字符串）。
        prompt_tokens: 输入 token 数（降级时为 0）。
        completion_tokens: 输出 token 数（降级时为 0）。
        latency_ms: 端到端耗时（毫秒）。
        error: 失败原因；成功时为 None。
    """

    sections: list[ReportSection] = Field(default_factory=list)
    report_period: str = Field(default="")
    fallback: bool = Field(default=False, description="LLM 失败时为 True")
    raw_text: str = Field(default="", description="LLM 原始输出；降级时为空")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0.0, ge=0.0)
    error: str | None = Field(default=None)

    @property
    def success(self) -> bool:
        """报告是否由 LLM 生成成功（非降级）。"""
        return not self.fallback and self.error is None

    @property
    def all_sections_present(self) -> bool:
        """5 段是否齐全且 content 非空。"""
        if len(self.sections) != 5:
            return False
        return all(s.content.strip() for s in self.sections)

    def to_markdown(self) -> str:
        """渲染为完整 Markdown 文本（M3.07 WeasyPrint 消费）。

        Returns:
            ``# {title1}\n\n{content1}\n\n# {title2}\n\n{content2}\n\n...``
        """
        parts: list[str] = []
        for section in self.sections:
            parts.append(f"# {section.title}\n\n{section.content}")
        return "\n\n".join(parts)


__all__ = [
    "ReportResult",
    "ReportSection",
    "ReportSectionType",
]
