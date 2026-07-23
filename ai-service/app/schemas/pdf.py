"""M10 报告 PDF schemas（spec §2.3 M10 + plan M3.07）。

定义 PdfConverter 的输出契约：

* ``PdfResult`` — PDF 转换结果（bytes + 元数据 + 降级标记）。

设计要点：

1. **PDF bytes 输出** — ``PdfResult.pdf_bytes`` 为 ``bytes``，便于 L2
   ``ReportArtifactWriter``（M3.08）直接上传 MinIO 而无需中转文件。
2. **降级标记** — ``fallback=True`` 表示 WeasyPrint 失败时由占位逻辑
   生成的最小 PDF（仅含标题 + 失败原因文本），前端可据此提示用户
   「PDF 生成失败」。
3. **页面规格** — 默认 A4（210×297mm），对齐 spec §2.3 M10 报告排版。
4. **图表数量** — ``chart_count`` 字段透传实际嵌入 PDF 的图表数；
   便于 M3.10 SLA 度量与 M3.09 前端展示。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PdfResult(BaseModel):
    """Markdown → PDF 转换结果。

    Attributes:
        pdf_bytes: PDF 二进制内容；降级时为最小占位 PDF。
        fallback: 是否为降级占位 PDF；True 表示 WeasyPrint 失败时生成。
        error: 失败原因；成功时为 None。
        chart_count: 实际嵌入 PDF 的图表数量（0-3）。
        page_size: 页面规格（如 "A4"）；用于 M3.09 前端展示。
        latency_ms: 端到端耗时（毫秒）。
    """

    pdf_bytes: bytes = Field(default=b"", description="PDF 二进制内容")
    fallback: bool = Field(default=False, description="WeasyPrint 失败时为 True")
    error: str | None = Field(default=None)
    chart_count: int = Field(default=0, ge=0, le=3)
    page_size: str = Field(default="A4", description="页面规格")
    latency_ms: float = Field(default=0.0, ge=0.0)

    @property
    def success(self) -> bool:
        """是否由 WeasyPrint 正常生成（非降级）。"""
        return not self.fallback and self.error is None and len(self.pdf_bytes) > 0


__all__ = ["PdfResult"]
