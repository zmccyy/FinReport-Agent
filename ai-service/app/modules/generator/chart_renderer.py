"""M10 ChartRenderer 服务端渲染（spec §2.3 M10 + plan M3.06）。

职责：

* 基于 ``FinancialStatement`` 数据，渲染 3 张 PNG 图表：

  1. 资产结构饼图（资产负债表关键科目占比）
  2. 营收趋势折线（多期营业收入对比）
  3. 现金流柱状图（经营 / 投资 / 筹资三类现金流净额）

* 失败降级（spec §10.3）：数据缺失 / 渲染异常 / 字体缺失 都不抛，
  返回占位 PNG（``fallback=True``），保证 Reasoner → Report 链路不被
  图表渲染失败阻断。

设计要点：

1. **matplotlib + Agg 后端** — 服务端无 GUI，必须用 ``matplotlib.use('Agg')``
   在导入 pyplot 前设置；PNG bytes 通过 ``Figure.savefig(BytesIO, format='png')``
   输出，不落临时文件。
2. **尺寸 800×500** — DPI 100 时 figure size = (8, 5) 英寸，对齐 plan M3.06
   验收标准。
3. **中文字体降级** — 尝试 SimHei / Microsoft YaHei / WenQuanYi Zen Hei 等
   常见中文字体；找不到时 fallback 到英文标签（避免 matplotlib ``Glyph missing``
   警告渲染成方框）。
4. **不可变输入** — 不修改 ``FinancialStatement``；产出新的 ``ChartResult``。
5. **数据提取同义词组** — 与 ``accounting_rules.py`` / ``anomaly_detector.py``
   风格一致，容忍 A 股年报科目名变体（如「营业收入」 vs 「营业总收入」）。
6. **Decimal 精度** — 图表数据用 ``Decimal`` 计算避免 float 累积误差
   （spec §8.4）；matplotlib 接受 float 输入时统一 ``float()`` 转换。
7. **降级占位 PNG** — 渲染失败时由 ``_render_placeholder_png`` 生成一张
   含错误信息的占位图，便于前端展示「图表生成失败」而非空白。
"""

from __future__ import annotations

import io
from decimal import Decimal

# 注意：此处不导入 numpy / matplotlib。pyplot 与 font_manager 在函数内
# 延迟导入，避免模块级 coverage hook 触发 numpy 二次导入（Win + numpy 2.x
# 与 matplotlib 3.10.x 已知兼容性问题，pytest-cov 会二次 patch 已导入
# 模块，导致 numpy _NoValue sentinel 与 C 扩展不一致，add_subplot 抛
# TypeError）。

from app.schemas.chart import ChartResult, ChartSpec, ChartType
from app.schemas.statement import (
    FinancialStatement,
    Period,
    StatementItem,
    StatementType,
)
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# 中文字体候选列表（按优先级；找不到时 fallback 到英文标签）。
_CHINESE_FONTS_CANDIDATES = [
    "SimHei",  # Windows 黑体
    "Microsoft YaHei",  # Windows 微软雅黑
    "WenQuanYi Zen Hei",  # Linux 文泉驿正黑
    "WenQuanYi Micro Hei",  # Linux 文泉驿微米黑
    "PingFang SC",  # macOS 苹方简体
    "Heiti SC",  # macOS 黑体简体
    "STHeiti",  # macOS 华文黑体
    "Noto Sans CJK SC",  # Linux Noto 思源黑体
]

# 检测可用的中文字体；模块级延迟到首次 ChartRenderer 实例化时执行。
_APPLIED_FONT: str | None = None
_FONTS_INITIALIZED: bool = False


def _ensure_fonts_initialized() -> None:
    """惰性初始化中文字体检测 + rcParams 设置。

    在首次 ``ChartRenderer.__init__`` 时调用；避免模块级 import 触发
    numpy / matplotlib 加载，绕开 pytest-cov 的二次 patch 问题。
    """
    global _APPLIED_FONT, _FONTS_INITIALIZED
    if _FONTS_INITIALIZED:
        return
    _FONTS_INITIALIZED = True

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    for name in _CHINESE_FONTS_CANDIDATES:
        try:
            font_manager.findfont(
                font_manager.FontProperties(family=name),
                fallback_to_default=False,
            )
            _APPLIED_FONT = name
            break
        except Exception:  # noqa: BLE001
            continue

    if _APPLIED_FONT is not None:
        plt.rcParams["font.sans-serif"] = [_APPLIED_FONT, "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False  # 负号正常显示


# 图表默认尺寸（plan M3.06 验收：800×500，DPI 100 → figure 8×5 英寸）。
_DEFAULT_WIDTH_PX = 800
_DEFAULT_HEIGHT_PX = 500
_DEFAULT_DPI = 100

# 同义词组：资产结构饼图关键科目（资产负债表）。
_ASSET_PIE_SYNONYMS: dict[str, list[str]] = {
    "货币资金": ["货币资金", "现金及存放中央银行款项"],
    "应收账款": ["应收账款", "应收账款净额"],
    "存货": ["存货", "存货净额"],
    "固定资产": ["固定资产", "固定资产净额"],
    "无形资产": ["无形资产", "无形资产净额"],
}

# 合计科目（不应被合并到「其他」切片中，否则会导致饼图比例重复计算）。
# 这些科目本身是其他科目的累加值，若加入「其他」会让总切片值翻倍。
_TOTAL_ITEM_NAMES: set[str] = {
    "资产总计",
    "负债合计",
    "所有者权益合计",
    "负债和所有者权益合计",
    "所有者权益合计（股东权益）",
    "归属于母公司所有者权益合计",
    "少数股东权益",
}

# 同义词组：营收趋势（利润表）。
_REVENUE_SYNONYMS: list[str] = ["营业收入", "营业总收入"]

# 同义词组：现金流（现金流量表）。
_CASH_FLOW_SYNONYMS: dict[str, list[str]] = {
    "经营活动": ["经营活动产生的现金流量净额", "经营活动现金流量净额"],
    "投资活动": ["投资活动产生的现金流量净额", "投资活动现金流量净额"],
    "筹资活动": [
        "筹资活动产生的现金流量净额",
        "筹资活动现金流量净额",
        "融资活动产生的现金流量净额",
    ],
}


class ChartRenderer:
    """3 张财报图表服务端渲染（spec §2.3 M10）。

    Attributes:
        width_px: 输出 PNG 宽度（像素）。
        height_px: 输出 PNG 高度（像素）。
        dpi: 渲染 DPI。
        use_chinese_label: 是否使用中文标签；找不到中文字体时自动降级为 False。
    """

    def __init__(
        self,
        *,
        width_px: int = _DEFAULT_WIDTH_PX,
        height_px: int = _DEFAULT_HEIGHT_PX,
        dpi: int = _DEFAULT_DPI,
        use_chinese_label: bool = True,
    ) -> None:
        """初始化图表渲染器。

        Args:
            width_px: 输出 PNG 宽度。
            height_px: 输出 PNG 高度。
            dpi: 渲染 DPI。
            use_chinese_label: 是否尝试使用中文标签；找不到中文字体时自动降级。
        """
        self.width_px = width_px
        self.height_px = height_px
        self.dpi = dpi
        # 惰性初始化字体检测（避免模块级触发 numpy / matplotlib 加载）。
        _ensure_fonts_initialized()
        self._chinese_font = _APPLIED_FONT if use_chinese_label else None
        self.use_chinese_label = self._chinese_font is not None
        if use_chinese_label and not self.use_chinese_label:
            LOGGER.warning(
                "[ChartRenderer] 未找到中文字体，回退到英文标签；"
                "建议安装 SimHei / Microsoft YaHei / Noto Sans CJK SC。"
            )

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def render_asset_structure_pie(self, statement: FinancialStatement) -> ChartResult:
        """渲染资产结构饼图（资产负债表关键科目占比）。

        Args:
            statement: 财务报表数据。

        Returns:
            ``ChartResult``；失败时 ``fallback=True`` + 占位 PNG。
        """
        spec = ChartSpec(chart_type=ChartType.ASSET_STRUCTURE_PIE)
        title = spec.resolve_title()
        try:
            items = statement.statements.get(StatementType.BALANCE_SHEET, [])
            if not items:
                return self._fallback(spec, title, "资产负债表数据缺失")

            slices = _extract_asset_pie_slices(items)
            if not slices:
                return self._fallback(spec, title, "未找到资产结构关键科目")

            labels, values = self._format_pie_slices(slices)
            fig = _new_figure(self.width_px, self.height_px, self.dpi)
            try:
                ax = fig.add_subplot(1, 1, 1)
                ax.pie(
                    values,
                    labels=labels,
                    autopct="%1.1f%%",
                    startangle=90,
                    textprops={"fontsize": 10},
                )
                ax.set_title(title, fontsize=14)
                ax.axis("equal")
                png_bytes = _figure_to_png(fig)
            finally:
                _close_figure(fig)

            LOGGER.info(
                "[ChartRenderer.render_asset_structure_pie] success slices=%d",
                len(slices),
            )
            return ChartResult(
                chart_type=ChartType.ASSET_STRUCTURE_PIE,
                title=title,
                png_bytes=png_bytes,
                width=self.width_px,
                height=self.height_px,
                fallback=False,
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 — 任何异常都降级
            LOGGER.warning(
                "[ChartRenderer.render_asset_structure_pie] 渲染失败: %s",
                exc,
            )
            return self._fallback(spec, title, f"渲染异常: {exc}")

    def render_revenue_trend_line(self, statement: FinancialStatement) -> ChartResult:
        """渲染营收趋势折线（多期营业收入对比）。

        Args:
            statement: 财务报表数据。

        Returns:
            ``ChartResult``；失败时 ``fallback=True`` + 占位 PNG。
        """
        spec = ChartSpec(chart_type=ChartType.REVENUE_TREND_LINE)
        title = spec.resolve_title()
        try:
            items = statement.statements.get(StatementType.INCOME_STATEMENT, [])
            if not items:
                return self._fallback(spec, title, "利润表数据缺失")

            revenue_values = _extract_revenue_values(items)
            if not revenue_values:
                return self._fallback(spec, title, "未找到营业收入科目")

            labels = self._revenue_labels(len(revenue_values))
            fig = _new_figure(self.width_px, self.height_px, self.dpi)
            try:
                ax = fig.add_subplot(1, 1, 1)
                ax.plot(
                    labels,
                    [float(v) for v in revenue_values],
                    marker="o",
                    linewidth=2,
                    color="#2E86AB",
                )
                ax.set_title(title, fontsize=14)
                ax.set_ylabel(self._label_revenue_y(statement.unit), fontsize=11)
                ax.grid(True, linestyle="--", alpha=0.5)
                # 数值标注
                for x, y in zip(labels, revenue_values):
                    ax.annotate(
                        _format_decimal_short(y),
                        xy=(x, float(y)),
                        xytext=(0, 8),
                        textcoords="offset points",
                        ha="center",
                        fontsize=9,
                    )
                png_bytes = _figure_to_png(fig)
            finally:
                _close_figure(fig)

            LOGGER.info(
                "[ChartRenderer.render_revenue_trend_line] success points=%d",
                len(revenue_values),
            )
            return ChartResult(
                chart_type=ChartType.REVENUE_TREND_LINE,
                title=title,
                png_bytes=png_bytes,
                width=self.width_px,
                height=self.height_px,
                fallback=False,
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "[ChartRenderer.render_revenue_trend_line] 渲染失败: %s",
                exc,
            )
            return self._fallback(spec, title, f"渲染异常: {exc}")

    def render_cash_flow_bar(self, statement: FinancialStatement) -> ChartResult:
        """渲染现金流柱状图（经营 / 投资 / 筹资三类净额）。

        Args:
            statement: 财务报表数据。

        Returns:
            ``ChartResult``；失败时 ``fallback=True`` + 占位 PNG。
        """
        spec = ChartSpec(chart_type=ChartType.CASH_FLOW_BAR)
        title = spec.resolve_title()
        try:
            items = statement.statements.get(StatementType.CASH_FLOW, [])
            if not items:
                return self._fallback(spec, title, "现金流量表数据缺失")

            bars = _extract_cash_flow_bars(items)
            if not bars:
                return self._fallback(spec, title, "未找到现金流量净额科目")

            labels = self._cash_flow_labels(len(bars))
            values = [float(b[1]) for b in bars]
            colors = ["#2E86AB" if v >= 0 else "#E63946" for v in values]
            fig = _new_figure(self.width_px, self.height_px, self.dpi)
            try:
                ax = fig.add_subplot(1, 1, 1)
                ax.bar(labels, values, color=colors, edgecolor="black", linewidth=0.5)
                ax.set_title(title, fontsize=14)
                ax.set_ylabel(self._label_cash_flow_y(statement.unit), fontsize=11)
                ax.axhline(y=0, color="black", linewidth=0.8)
                ax.grid(True, axis="y", linestyle="--", alpha=0.5)
                # 数值标注
                for x, y in zip(labels, values):
                    offset = 8 if y >= 0 else -16
                    ax.annotate(
                        _format_decimal_short(Decimal(str(y))),
                        xy=(x, y),
                        xytext=(0, offset),
                        textcoords="offset points",
                        ha="center",
                        fontsize=9,
                    )
                png_bytes = _figure_to_png(fig)
            finally:
                _close_figure(fig)

            LOGGER.info(
                "[ChartRenderer.render_cash_flow_bar] success bars=%d",
                len(bars),
            )
            return ChartResult(
                chart_type=ChartType.CASH_FLOW_BAR,
                title=title,
                png_bytes=png_bytes,
                width=self.width_px,
                height=self.height_px,
                fallback=False,
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "[ChartRenderer.render_cash_flow_bar] 渲染失败: %s",
                exc,
            )
            return self._fallback(spec, title, f"渲染异常: {exc}")

    def render_all(self, statement: FinancialStatement) -> list[ChartResult]:
        """渲染全部 3 张图表。

        Args:
            statement: 财务报表数据。

        Returns:
            长度固定为 3 的 ``ChartResult`` 列表，按 ``ChartType`` 枚举顺序：
            [asset_structure_pie, revenue_trend_line, cash_flow_bar]。
        """
        return [
            self.render_asset_structure_pie(statement),
            self.render_revenue_trend_line(statement),
            self.render_cash_flow_bar(statement),
        ]

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _fallback(self, spec: ChartSpec, title: str, reason: str) -> ChartResult:
        """生成降级占位 PNG。"""
        png_bytes = _render_placeholder_png(
            title=title,
            reason=reason,
            width=self.width_px,
            height=self.height_px,
            dpi=self.dpi,
        )
        return ChartResult(
            chart_type=spec.chart_type,
            title=title,
            png_bytes=png_bytes,
            width=self.width_px,
            height=self.height_px,
            fallback=True,
            error=reason,
        )

    def _format_pie_slices(
        self, slices: list[tuple[str, Decimal]]
    ) -> tuple[list[str], list[float]]:
        """格式化饼图标签 + 数值列表。

        标签直接来自 ``_extract_asset_pie_slices`` 返回的名称（已是
        「货币资金 / 应收账款 / ... / 其他」中文，或英文场景下由调用方
        传入的英文名）。
        """
        labels = [name for name, _ in slices]
        values = [float(v) for _, v in slices]
        return labels, values

    def _revenue_labels(self, count: int) -> list[str]:
        """生成营收折线 X 轴标签。"""
        if self.use_chinese_label:
            base = ["本期", "上期", "去年同期"]
        else:
            base = ["Current", "Prior", "YoY"]
        if count <= len(base):
            return base[:count]
        # 超过 3 期时用「期 1 / 期 2 / ...」
        if self.use_chinese_label:
            return [f"期 {i + 1}" for i in range(count)]
        return [f"Period {i + 1}" for i in range(count)]

    def _cash_flow_labels(self, count: int) -> list[str]:
        """生成现金流柱状 X 轴标签。"""
        if self.use_chinese_label:
            base = ["经营活动", "投资活动", "筹资活动"]
        else:
            base = ["Operating", "Investing", "Financing"]
        return base[:count]

    def _label_revenue_y(self, unit: str) -> str:
        """营收 Y 轴标签。"""
        if self.use_chinese_label:
            return f"金额（{unit}）"
        return f"Amount ({unit})"

    def _label_cash_flow_y(self, unit: str) -> str:
        """现金流 Y 轴标签。"""
        if self.use_chinese_label:
            return f"净额（{unit}）"
        return f"Net ({unit})"


# ============================================================================
# 模块级辅助函数
# ============================================================================


def _new_figure(width_px: int, height_px: int, dpi: int):
    """创建新 figure（不使用 pyplot 全局状态，便于线程安全）。"""
    import matplotlib.pyplot as plt

    figsize = (width_px / dpi, height_px / dpi)
    fig = plt.figure(figsize=figsize, dpi=dpi)
    # 中文字体设置由 _ensure_fonts_initialized 设置的 rcParams 全局应用。
    return fig


def _close_figure(fig) -> None:
    """关闭 figure，释放 matplotlib 资源。"""
    import matplotlib.pyplot as plt

    plt.close(fig)


def _figure_to_png(fig) -> bytes:
    """将 figure 渲染为 PNG bytes。

    Args:
        fig: matplotlib Figure。

    Returns:
        PNG 二进制内容。
    """
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=fig.dpi, bbox_inches="tight")
    return buf.getvalue()


def _render_placeholder_png(
    *,
    title: str,
    reason: str,
    width: int,
    height: int,
    dpi: int,
) -> bytes:
    """渲染降级占位 PNG（含标题 + 失败原因）。

    Args:
        title: 图表标题。
        reason: 失败原因。
        width: 宽度（像素）。
        height: 高度（像素）。
        dpi: DPI。

    Returns:
        PNG 二进制内容。
    """
    import matplotlib.pyplot as plt

    figsize = (width / dpi, height / dpi)
    fig = plt.figure(figsize=figsize, dpi=dpi)
    try:
        ax = fig.add_subplot(1, 1, 1)
        ax.set_title(title, fontsize=14, color="#666666")
        ax.text(
            0.5,
            0.5,
            f"图表数据不可用\n{reason}",
            ha="center",
            va="center",
            fontsize=12,
            color="#999999",
            wrap=True,
        )
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
        return buf.getvalue()
    finally:
        plt.close(fig)


# ============================================================================
# 数据提取辅助
# ============================================================================


def _find_item_value(items: list[StatementItem], synonyms: list[str]) -> Decimal | None:
    """按同义词组查询科目值；任一命中即返回。

    Args:
        items: ``StatementItem`` 列表。
        synonyms: 同义词列表。

    Returns:
        ``Decimal`` 值；未命中返回 None。
    """
    if not synonyms:
        return None
    name_to_value: dict[str, Decimal] = {item.item: Decimal(str(item.value)) for item in items}
    for name in synonyms:
        if name in name_to_value:
            return name_to_value[name]
    return None


def _extract_asset_pie_slices(
    items: list[StatementItem],
) -> list[tuple[str, Decimal]]:
    """提取资产结构饼图切片。

    Args:
        items: 资产负债表科目列表。

    Returns:
        (name, value) 元组列表；按预定义关键科目顺序。
        剩余非合计科目合并为「其他」。

    Note:
        合计科目（资产总计 / 负债合计 / 所有者权益合计 等）会被跳过，
        避免被并入「其他」导致饼图比例重复计算。
    """
    slices: list[tuple[str, Decimal]] = []
    used_names: set[str] = set()

    for label, synonyms in _ASSET_PIE_SYNONYMS.items():
        value = _find_item_value(items, synonyms)
        if value is not None and value != Decimal("0"):
            slices.append((label, value))
            # 记录该同义词组所有可能名称，避免后面再被合并到「其他」
            for name in synonyms:
                used_names.add(name)

    # 合并剩余非零非合计科目为「其他」
    other_total = Decimal("0")
    for item in items:
        if item.item in used_names:
            continue
        if item.item in _TOTAL_ITEM_NAMES:
            continue
        try:
            v = Decimal(str(item.value))
        except Exception:  # noqa: BLE001
            continue
        if v != Decimal("0"):
            other_total += v
    if other_total != Decimal("0"):
        slices.append(("其他", other_total))

    return slices


def _extract_revenue_values(items: list[StatementItem]) -> list[Decimal]:
    """提取营业收入多期数值（spec §2.3 M10 营收趋势折线）。

    按 ``StatementItem.period`` 区分本期 / 上期 / 上年同期，按时间顺序
    返回。同一 period 仅取第一条匹配的营业收入科目（避免重复）。

    Args:
        items: 利润表科目列表。

    Returns:
        营业收入数值列表，按 本期 → 上期 → 上年同期 顺序；无数据时为空。
    """
    # period 优先级（值越小越靠前）
    period_order = {
        Period.CURRENT: 0,
        Period.PRIOR: 1,
        Period.PRIOR_YEAR_TO_DATE: 2,
    }
    revenue_by_period: dict[Period, Decimal] = {}
    for item in items:
        if item.item not in _REVENUE_SYNONYMS:
            continue
        if item.period not in period_order:
            continue
        # 同一 period 只取第一条，避免 extractor 重复抽取
        if item.period in revenue_by_period:
            continue
        revenue_by_period[item.period] = Decimal(str(item.value))
    return [
        revenue_by_period[p]
        for p in sorted(revenue_by_period.keys(), key=lambda x: period_order[x])
    ]


def _extract_cash_flow_bars(
    items: list[StatementItem],
) -> list[tuple[str, Decimal]]:
    """提取现金流柱状图数据。

    Args:
        items: 现金流量表科目列表。

    Returns:
        (label, value) 元组列表，按经营 / 投资 / 筹资顺序。
    """
    bars: list[tuple[str, Decimal]] = []
    for label, synonyms in _CASH_FLOW_SYNONYMS.items():
        value = _find_item_value(items, synonyms)
        if value is not None:
            bars.append((label, value))
    return bars


def _format_decimal_short(v: Decimal) -> str:
    """格式化 Decimal 为短字符串（用于图表数值标注）。

    大数自动转「亿 / 万」单位，避免标注过长。

    Args:
        v: Decimal 数值。

    Returns:
        短字符串，如「1300.0 亿」「500.0 万」。
    """
    abs_v = abs(v)
    if abs_v >= Decimal("100000000"):  # 亿
        return f"{float(v / Decimal('100000000')):.1f} 亿"
    if abs_v >= Decimal("10000"):  # 万
        return f"{float(v / Decimal('10000')):.1f} 万"
    return f"{float(v):.2f}"


__all__ = ["ChartRenderer"]
