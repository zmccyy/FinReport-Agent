"""M10 报告图表 schemas（spec §2.3 M10 + plan M3.06）。

定义 ChartRenderer 的输入 / 输出契约：

* ``ChartType`` — 3 张固定图表枚举（资产结构饼图 / 营收趋势折线 / 现金流柱状）。
* ``ChartSpec`` — 单张图表规格（类型 + 标题 + 数据源描述）。
* ``ChartResult`` — 渲染结果（PNG bytes + 尺寸 + 降级标记 + 错误原因）。

设计要点：

1. **3 张固定图表** — 对齐 spec §2.3 M10：

   1. 资产结构饼图（资产负债表关键科目占比）
   2. 营收趋势折线（本期 vs 上期 / 同期，多期营收对比）
   3. 现金流柱状图（经营 / 投资 / 筹资三类现金流净额）

2. **PNG bytes 输出** — ``ChartResult.png_bytes`` 为 ``bytes``，便于 L2
   ``ReportArtifactWriter``（M3.08）直接上传 MinIO 而无需中转文件。
3. **尺寸固定 800×500** — 对齐 plan M3.06 验收标准；DPI 100 时
   matplotlib figure size = (8, 5) 英寸。
4. **降级标记** — ``fallback=True`` 表示数据缺失或渲染异常时生成的占位 PNG，
   前端可据此提示用户「图表数据不完整」。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ChartType(str, Enum):
    """3 张固定图表类型（spec §2.3 M10 + plan M3.06）。"""

    ASSET_STRUCTURE_PIE = "asset_structure_pie"
    """资产结构饼图 — 资产负债表关键科目占比"""

    REVENUE_TREND_LINE = "revenue_trend_line"
    """营收趋势折线 — 多期营业收入对比"""

    CASH_FLOW_BAR = "cash_flow_bar"
    """现金流柱状图 — 经营 / 投资 / 筹资三类现金流净额"""

    @property
    def chinese_name(self) -> str:
        """图表中文名（默认 ``ChartSpec.title``）。"""
        labels = {
            ChartType.ASSET_STRUCTURE_PIE: "资产结构饼图",
            ChartType.REVENUE_TREND_LINE: "营收趋势折线",
            ChartType.CASH_FLOW_BAR: "现金流柱状图",
        }
        return labels[self]


class ChartSpec(BaseModel):
    """单张图表规格。

    Attributes:
        chart_type: 图表类型枚举。
        title: 图表标题（默认对齐 ChartType.chinese_name）。
    """

    chart_type: ChartType
    title: str = Field(
        default="", description="图表标题；默认对齐 ChartType.chinese_name"
    )

    def resolve_title(self) -> str:
        """返回标题；空时回退到 ``ChartType.chinese_name``。"""
        return self.title or self.chart_type.chinese_name


class ChartResult(BaseModel):
    """单张图表渲染结果。

    Attributes:
        chart_type: 图表类型枚举。
        title: 实际使用的标题。
        png_bytes: PNG 二进制内容；降级时为占位 PNG。
        width: 图像宽度（像素），固定 800。
        height: 图像高度（像素），固定 500。
        fallback: 是否为降级占位 PNG；True 表示数据缺失或渲染异常。
        error: 失败原因；成功时为 None。
    """

    chart_type: ChartType
    title: str
    png_bytes: bytes = Field(default=b"", description="PNG 二进制内容")
    width: int = Field(default=800, ge=1)
    height: int = Field(default=500, ge=1)
    fallback: bool = Field(default=False, description="数据缺失或渲染异常时为 True")
    error: str | None = Field(default=None)

    @property
    def success(self) -> bool:
        """是否由数据正常渲染（非降级）。"""
        return not self.fallback and self.error is None and len(self.png_bytes) > 0


__all__ = [
    "ChartResult",
    "ChartSpec",
    "ChartType",
]
