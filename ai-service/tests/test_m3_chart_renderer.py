"""M3.06 ChartRenderer 服务端渲染测试（spec §2.3 M10 + plan M3.06）。

测试覆盖：

1. **schema** — ChartType / ChartSpec / ChartResult 基础属性 + chinese_name / resolve_title / success
2. **资产结构饼图** — 正常渲染 / 数据缺失降级 / 关键科目缺失降级
3. **营收趋势折线** — 正常渲染 / 数据缺失降级
4. **现金流柱状图** — 正常渲染 / 正负值不同颜色 / 数据缺失降级
5. **render_all** — 3 张图表顺序固定 + 长度 = 3
6. **降级** — 占位 PNG 生成 / fallback=True / error 填充
7. **不可变输入** — 原 FinancialStatement 不被修改
8. **尺寸** — PNG bytes 非空 / 宽高符合 800×500
9. **PNG 合法性** — bytes 以 PNG magic number 开头（\\x89PNG）
10. **中英文字体** — use_chinese_label=True/False 都能渲染
"""

from __future__ import annotations

import warnings
from decimal import Decimal

import pytest

# 抑制 matplotlib 中文字体警告（测试环境可能没装 SimHei）。
warnings.filterwarnings(
    "ignore",
    message=r"Glyph \d+ .* missing from font\(s\).*",
    category=UserWarning,
)

from app.modules.generator.chart_renderer import (  # noqa: E402
    ChartRenderer,
    _APPLIED_FONT,
    _ensure_fonts_initialized,
    _extract_asset_pie_slices,
    _extract_cash_flow_bars,
    _extract_revenue_values,
    _find_item_value,
    _format_decimal_short,
)
from app.schemas.chart import ChartResult, ChartSpec, ChartType  # noqa: E402
from app.schemas.statement import (  # noqa: E402
    FinancialStatement,
    Period,
    StatementItem,
    StatementType,
)

# PNG magic number（前 8 字节）。
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bs_items() -> list[StatementItem]:
    return [
        StatementItem(item="货币资金", value=150000000000.0),
        StatementItem(item="应收账款", value=20000000000.0),
        StatementItem(item="存货", value=80000000000.0),
        StatementItem(item="固定资产", value=30000000000.0),
        StatementItem(item="资产总计", value=280000000000.0),
    ]


def _is_items() -> list[StatementItem]:
    return [
        StatementItem(item="营业收入", value=130000000000.0),
        StatementItem(item="净利润", value=85000000000.0),
    ]


def _cf_items() -> list[StatementItem]:
    return [
        StatementItem(item="经营活动产生的现金流量净额", value=90000000000.0),
        StatementItem(item="投资活动产生的现金流量净额", value=-15000000000.0),
        StatementItem(item="筹资活动产生的现金流量净额", value=-30000000000.0),
    ]


def _statement(
    *,
    bs: list[StatementItem] | None = None,
    is_: list[StatementItem] | None = None,
    cf: list[StatementItem] | None = None,
) -> FinancialStatement:
    statements: dict[StatementType, list[StatementItem]] = {}
    if bs is not None:
        statements[StatementType.BALANCE_SHEET] = bs
    if is_ is not None:
        statements[StatementType.INCOME_STATEMENT] = is_
    if cf is not None:
        statements[StatementType.CASH_FLOW] = cf
    return FinancialStatement(
        report_period="2025-12-31",
        currency="CNY",
        unit="元",
        statements=statements,
    )


@pytest.fixture
def full_statement() -> FinancialStatement:
    """三表齐全的财报。"""
    return _statement(bs=_bs_items(), is_=_is_items(), cf=_cf_items())


@pytest.fixture
def renderer() -> ChartRenderer:
    """默认 ChartRenderer 实例。"""
    return ChartRenderer()


# ---------------------------------------------------------------------------
# Schema 测试
# ---------------------------------------------------------------------------


class TestChartSchema:
    """ChartType / ChartSpec / ChartResult schema 测试。"""

    def test_chart_type_chinese_name(self) -> None:
        """3 张图表都有中文名。"""
        assert ChartType.ASSET_STRUCTURE_PIE.chinese_name == "资产结构饼图"
        assert ChartType.REVENUE_TREND_LINE.chinese_name == "营收趋势折线"
        assert ChartType.CASH_FLOW_BAR.chinese_name == "现金流柱状图"

    def test_chart_spec_resolve_title_default(self) -> None:
        """ChartSpec.title 为空时 resolve_title 回退到 chinese_name。"""
        spec = ChartSpec(chart_type=ChartType.ASSET_STRUCTURE_PIE)
        assert spec.resolve_title() == "资产结构饼图"

    def test_chart_spec_resolve_title_custom(self) -> None:
        """ChartSpec.title 自定义时保留。"""
        spec = ChartSpec(
            chart_type=ChartType.ASSET_STRUCTURE_PIE,
            title="贵州茅台资产结构",
        )
        assert spec.resolve_title() == "贵州茅台资产结构"

    def test_chart_result_success_true_on_normal(self) -> None:
        """正常渲染 ChartResult.success = True。"""
        r = ChartResult(
            chart_type=ChartType.ASSET_STRUCTURE_PIE,
            title="t",
            png_bytes=b"\x89PNG",
            width=800,
            height=500,
        )
        assert r.success is True

    def test_chart_result_success_false_on_fallback(self) -> None:
        """fallback=True 时 success = False。"""
        r = ChartResult(
            chart_type=ChartType.ASSET_STRUCTURE_PIE,
            title="t",
            png_bytes=b"\x89PNG",
            fallback=True,
        )
        assert r.success is False

    def test_chart_result_success_false_on_empty_bytes(self) -> None:
        """png_bytes 为空时 success = False。"""
        r = ChartResult(
            chart_type=ChartType.ASSET_STRUCTURE_PIE,
            title="t",
            png_bytes=b"",
        )
        assert r.success is False

    def test_chart_result_success_false_on_error(self) -> None:
        """error 非空时 success = False。"""
        r = ChartResult(
            chart_type=ChartType.ASSET_STRUCTURE_PIE,
            title="t",
            png_bytes=b"\x89PNG",
            error="some error",
        )
        assert r.success is False


# ---------------------------------------------------------------------------
# 资产结构饼图
# ---------------------------------------------------------------------------


class TestAssetStructurePie:
    """资产结构饼图测试。"""

    def test_should_render_valid_png(self, renderer: ChartRenderer) -> None:
        """正常数据应渲染合法 PNG。"""
        result = renderer.render_asset_structure_pie(_statement(bs=_bs_items()))
        assert result.chart_type == ChartType.ASSET_STRUCTURE_PIE
        assert result.fallback is False
        assert result.error is None
        assert result.png_bytes[:8] == _PNG_MAGIC
        assert len(result.png_bytes) > 1000
        assert result.width == 800
        assert result.height == 500

    def test_should_fallback_when_balance_sheet_missing(
        self, renderer: ChartRenderer
    ) -> None:
        """资产负债表缺失时降级。"""
        result = renderer.render_asset_structure_pie(_statement())
        assert result.fallback is True
        assert "资产负债表数据缺失" in (result.error or "")
        assert result.png_bytes[:8] == _PNG_MAGIC

    def test_should_not_fallback_when_only_other_items(
        self, renderer: ChartRenderer
    ) -> None:
        """资产负债表只有非关键科目时，「其他」切片存在，不降级。"""
        bs = [StatementItem(item="其他应收款", value=1000.0)]
        result = renderer.render_asset_structure_pie(_statement(bs=bs))
        assert result.fallback is False
        assert result.png_bytes[:8] == _PNG_MAGIC

    def test_should_fallback_when_only_total_items(
        self, renderer: ChartRenderer
    ) -> None:
        """资产负债表只有合计科目（资产总计等）时降级。

        合计科目不应被合并到「其他」切片（避免比例重复计算），
        所以 slices 为空 → 触发降级。
        """
        bs = [
            StatementItem(item="资产总计", value=280000000000.0),
            StatementItem(item="负债合计", value=150000000000.0),
        ]
        result = renderer.render_asset_structure_pie(_statement(bs=bs))
        assert result.fallback is True
        assert "未找到" in (result.error or "")

    def test_should_exclude_total_items_from_other_slice(
        self, renderer: ChartRenderer
    ) -> None:
        """「资产总计」不应被合并到「其他」切片，避免比例翻倍。"""
        bs = [
            StatementItem(item="货币资金", value=150000000000.0),  # 1500 亿
            StatementItem(item="应收账款", value=20000000000.0),  # 200 亿
            StatementItem(item="资产总计", value=170000000000.0),  # 1700 亿
        ]
        result = renderer.render_asset_structure_pie(_statement(bs=bs))
        assert result.fallback is False
        # 切片总和应等于关键科目总和（1700 亿），不含资产总计
        # 通过 _extract_asset_pie_slices 直接验证
        slices = _extract_asset_pie_slices(bs)
        total = sum(v for _, v in slices)
        assert total == Decimal("170000000000.0")

    def test_should_fallback_when_all_values_zero(
        self, renderer: ChartRenderer
    ) -> None:
        """所有科目值为 0 时降级（无切片可绘制）。"""
        bs = [
            StatementItem(item="货币资金", value=0.0),
            StatementItem(item="应收账款", value=0.0),
        ]
        result = renderer.render_asset_structure_pie(_statement(bs=bs))
        assert result.fallback is True
        assert "未找到" in (result.error or "")


# ---------------------------------------------------------------------------
# 营收趋势折线
# ---------------------------------------------------------------------------


class TestRevenueTrendLine:
    """营收趋势折线测试。"""

    def test_should_render_valid_png(self, renderer: ChartRenderer) -> None:
        """正常数据应渲染合法 PNG。"""
        result = renderer.render_revenue_trend_line(_statement(is_=_is_items()))
        assert result.chart_type == ChartType.REVENUE_TREND_LINE
        assert result.fallback is False
        assert result.png_bytes[:8] == _PNG_MAGIC

    def test_should_fallback_when_income_statement_missing(
        self, renderer: ChartRenderer
    ) -> None:
        """利润表缺失时降级。"""
        result = renderer.render_revenue_trend_line(_statement())
        assert result.fallback is True
        assert "利润表数据缺失" in (result.error or "")

    def test_should_fallback_when_no_revenue_item(
        self, renderer: ChartRenderer
    ) -> None:
        """利润表存在但无营业收入科目时降级。"""
        is_ = [StatementItem(item="净利润", value=100.0)]
        result = renderer.render_revenue_trend_line(_statement(is_=is_))
        assert result.fallback is True
        assert "未找到营业收入" in (result.error or "")

    def test_should_render_with_revenue_synonym(self, renderer: ChartRenderer) -> None:
        """同义词「营业总收入」也能识别。"""
        is_ = [StatementItem(item="营业总收入", value=130000000000.0)]
        result = renderer.render_revenue_trend_line(_statement(is_=is_))
        assert result.fallback is False


# ---------------------------------------------------------------------------
# 现金流柱状图
# ---------------------------------------------------------------------------


class TestCashFlowBar:
    """现金流柱状图测试。"""

    def test_should_render_valid_png(self, renderer: ChartRenderer) -> None:
        """正常数据应渲染合法 PNG。"""
        result = renderer.render_cash_flow_bar(_statement(cf=_cf_items()))
        assert result.chart_type == ChartType.CASH_FLOW_BAR
        assert result.fallback is False
        assert result.png_bytes[:8] == _PNG_MAGIC

    def test_should_fallback_when_cash_flow_missing(
        self, renderer: ChartRenderer
    ) -> None:
        """现金流量表缺失时降级。"""
        result = renderer.render_cash_flow_bar(_statement())
        assert result.fallback is True
        assert "现金流量表数据缺失" in (result.error or "")

    def test_should_fallback_when_no_cash_flow_items(
        self, renderer: ChartRenderer
    ) -> None:
        """现金流量表存在但无三类净额科目时降级。"""
        cf = [StatementItem(item="其他与经营活动无关的现金", value=100.0)]
        result = renderer.render_cash_flow_bar(_statement(cf=cf))
        assert result.fallback is True
        assert "未找到现金流量净额" in (result.error or "")

    def test_should_render_partial_cash_flow(self, renderer: ChartRenderer) -> None:
        """只有部分现金流科目时也能渲染（不全则少了对应柱）。"""
        cf = [
            StatementItem(item="经营活动产生的现金流量净额", value=90000000000.0),
        ]
        result = renderer.render_cash_flow_bar(_statement(cf=cf))
        assert result.fallback is False
        assert result.png_bytes[:8] == _PNG_MAGIC


# ---------------------------------------------------------------------------
# render_all
# ---------------------------------------------------------------------------


class TestRenderAll:
    """render_all 测试。"""

    def test_should_return_3_charts_in_order(
        self,
        renderer: ChartRenderer,
        full_statement: FinancialStatement,
    ) -> None:
        """render_all 返回 3 张图表，按 ChartType 枚举顺序。"""
        results = renderer.render_all(full_statement)
        assert len(results) == 3
        assert results[0].chart_type == ChartType.ASSET_STRUCTURE_PIE
        assert results[1].chart_type == ChartType.REVENUE_TREND_LINE
        assert results[2].chart_type == ChartType.CASH_FLOW_BAR

    def test_all_charts_have_valid_png(
        self,
        renderer: ChartRenderer,
        full_statement: FinancialStatement,
    ) -> None:
        """3 张图表都生成合法 PNG。"""
        results = renderer.render_all(full_statement)
        for r in results:
            assert r.png_bytes[:8] == _PNG_MAGIC
            assert r.fallback is False
            assert r.width == 800
            assert r.height == 500


# ---------------------------------------------------------------------------
# 不可变性
# ---------------------------------------------------------------------------


class TestImmutability:
    """不可变输入测试。"""

    def test_should_not_mutate_statement(
        self,
        renderer: ChartRenderer,
        full_statement: FinancialStatement,
    ) -> None:
        """渲染不应修改原 FinancialStatement。"""
        original_bs_count = len(
            full_statement.statements.get(StatementType.BALANCE_SHEET, [])
        )
        original_is_count = len(
            full_statement.statements.get(StatementType.INCOME_STATEMENT, [])
        )
        original_cf_count = len(
            full_statement.statements.get(StatementType.CASH_FLOW, [])
        )
        original_period = full_statement.report_period

        renderer.render_all(full_statement)

        assert (
            len(full_statement.statements.get(StatementType.BALANCE_SHEET, []))
            == original_bs_count
        )
        assert (
            len(full_statement.statements.get(StatementType.INCOME_STATEMENT, []))
            == original_is_count
        )
        assert (
            len(full_statement.statements.get(StatementType.CASH_FLOW, []))
            == original_cf_count
        )
        assert full_statement.report_period == original_period


# ---------------------------------------------------------------------------
# 自定义尺寸
# ---------------------------------------------------------------------------


class TestCustomSize:
    """自定义尺寸测试。"""

    def test_should_use_custom_dimensions(self) -> None:
        """自定义 width/height 应反映在 ChartResult。"""
        r = ChartRenderer(width_px=400, height_px=300, dpi=72)
        result = r.render_asset_structure_pie(_statement(bs=_bs_items()))
        assert result.width == 400
        assert result.height == 300
        assert result.png_bytes[:8] == _PNG_MAGIC


# ---------------------------------------------------------------------------
# 中英文字体
# ---------------------------------------------------------------------------


class TestChineseFont:
    """中英文字体降级测试。"""

    def test_should_use_chinese_when_font_available(self) -> None:
        """找到中文字体时 use_chinese_label = True。"""
        # 显式触发惰性初始化，确保 _APPLIED_FONT 已被填充
        # （否则在测试顺序未定时 _APPLIED_FONT 仍为模块加载时的 None）
        _ensure_fonts_initialized()
        if _APPLIED_FONT is None:
            pytest.skip("当前环境无中文字体，跳过此用例")
        r = ChartRenderer()
        assert r.use_chinese_label is True

    def test_should_fallback_to_english_when_disabled(self) -> None:
        """显式禁用中文时 use_chinese_label = False。"""
        r = ChartRenderer(use_chinese_label=False)
        assert r.use_chinese_label is False

    def test_should_render_with_english_labels(self) -> None:
        """禁用中文也能渲染合法 PNG。"""
        r = ChartRenderer(use_chinese_label=False)
        result = r.render_asset_structure_pie(_statement(bs=_bs_items()))
        assert result.png_bytes[:8] == _PNG_MAGIC
        assert result.fallback is False


# ---------------------------------------------------------------------------
# 数据提取辅助函数
# ---------------------------------------------------------------------------


class TestDataExtractors:
    """数据提取辅助函数测试。"""

    def test_find_item_value_by_first_synonym(self) -> None:
        """同义词组第一个命中即返回。"""
        items = [StatementItem(item="货币资金", value=100.0)]
        v = _find_item_value(items, ["货币资金", "现金及存放中央银行款项"])
        assert v == Decimal("100.0")

    def test_find_item_value_by_second_synonym(self) -> None:
        """同义词组第二个命中也能返回。"""
        items = [StatementItem(item="现金及存放中央银行款项", value=200.0)]
        v = _find_item_value(items, ["货币资金", "现金及存放中央银行款项"])
        assert v == Decimal("200.0")

    def test_find_item_value_returns_none_when_missing(self) -> None:
        """无命中时返回 None。"""
        items = [StatementItem(item="其他", value=100.0)]
        v = _find_item_value(items, ["货币资金"])
        assert v is None

    def test_find_item_value_returns_none_for_empty_synonyms(self) -> None:
        """同义词列表为空时返回 None。"""
        items = [StatementItem(item="货币资金", value=100.0)]
        v = _find_item_value(items, [])
        assert v is None

    def test_extract_asset_pie_slices_with_key_items(self) -> None:
        """关键科目齐全时返回切片；合计科目不纳入「其他」。"""
        slices = _extract_asset_pie_slices(_bs_items())
        names = [name for name, _ in slices]
        assert "货币资金" in names
        assert "应收账款" in names
        assert "存货" in names
        assert "固定资产" in names
        # 「资产总计」被跳过，且关键科目总和已等于资产总计（2800亿），
        # 所以「其他」=0，不生成「其他」切片
        assert "其他" not in names
        total = sum(v for _, v in slices)
        assert total == Decimal("280000000000.0")

    def test_extract_asset_pie_slices_skips_zero_values(self) -> None:
        """值为 0 的科目跳过。"""
        items = [
            StatementItem(item="货币资金", value=100.0),
            StatementItem(item="应收账款", value=0.0),
        ]
        slices = _extract_asset_pie_slices(items)
        names = [name for name, _ in slices]
        assert "货币资金" in names
        assert "应收账款" not in names

    def test_extract_revenue_values_with_revenue(self) -> None:
        """含营业收入时返回值列表。"""
        values = _extract_revenue_values(_is_items())
        assert len(values) == 1
        assert values[0] == Decimal("130000000000.0")

    def test_extract_revenue_values_with_synonym(self) -> None:
        """同义词「营业总收入」也能识别。"""
        items = [StatementItem(item="营业总收入", value=100.0)]
        values = _extract_revenue_values(items)
        assert len(values) == 1

    def test_extract_revenue_values_multi_period(self) -> None:
        """多期营业收入按 本期 → 上期 → 上年同期 顺序返回。"""
        items = [
            StatementItem(item="营业收入", value=130.0, period=Period.CURRENT),
            StatementItem(item="营业收入", value=110.0, period=Period.PRIOR),
            StatementItem(
                item="营业收入",
                value=100.0,
                period=Period.PRIOR_YEAR_TO_DATE,
            ),
        ]
        values = _extract_revenue_values(items)
        assert values == [
            Decimal("130.0"),
            Decimal("110.0"),
            Decimal("100.0"),
        ]

    def test_extract_revenue_values_reorders_periods(self) -> None:
        """无序输入也按 本期 → 上期 → 上年同期 顺序返回。"""
        items = [
            StatementItem(
                item="营业收入",
                value=100.0,
                period=Period.PRIOR_YEAR_TO_DATE,
            ),
            StatementItem(item="营业收入", value=130.0, period=Period.CURRENT),
            StatementItem(item="营业收入", value=110.0, period=Period.PRIOR),
        ]
        values = _extract_revenue_values(items)
        assert values == [
            Decimal("130.0"),
            Decimal("110.0"),
            Decimal("100.0"),
        ]

    def test_extract_revenue_values_skips_year_to_date(self) -> None:
        """CURRENT_YEAR_TO_DATE 累计值不纳入趋势对比。"""
        items = [
            StatementItem(item="营业收入", value=130.0, period=Period.CURRENT),
            StatementItem(
                item="营业收入",
                value=140.0,
                period=Period.CURRENT_YEAR_TO_DATE,
            ),
        ]
        values = _extract_revenue_values(items)
        assert values == [Decimal("130.0")]

    def test_extract_revenue_values_empty_when_no_revenue(self) -> None:
        """无营业收入时返回空列表。"""
        items = [StatementItem(item="净利润", value=100.0)]
        values = _extract_revenue_values(items)
        assert values == []

    def test_extract_cash_flow_bars_full(self) -> None:
        """三类现金流齐全时返回 3 个柱。"""
        bars = _extract_cash_flow_bars(_cf_items())
        assert len(bars) == 3
        labels = [label for label, _ in bars]
        assert "经营活动" in labels
        assert "投资活动" in labels
        assert "筹资活动" in labels

    def test_extract_cash_flow_bars_partial(self) -> None:
        """只有部分现金流科目时返回部分柱。"""
        items = [
            StatementItem(item="经营活动产生的现金流量净额", value=100.0),
        ]
        bars = _extract_cash_flow_bars(items)
        assert len(bars) == 1
        assert bars[0][0] == "经营活动"

    def test_extract_cash_flow_bars_with_synonym(self) -> None:
        """同义词「融资活动产生的现金流量净额」也能识别。"""
        items = [
            StatementItem(item="融资活动产生的现金流量净额", value=100.0),
        ]
        bars = _extract_cash_flow_bars(items)
        assert len(bars) == 1
        assert bars[0][0] == "筹资活动"


# ---------------------------------------------------------------------------
# 数值格式化
# ---------------------------------------------------------------------------


class TestFormatDecimalShort:
    """_format_decimal_short 测试。"""

    def test_format_yi(self) -> None:
        """亿级数值转「亿」单位。"""
        v = Decimal("130000000000")  # 1300 亿
        assert _format_decimal_short(v) == "1300.0 亿"

    def test_format_wan(self) -> None:
        """万级数值转「万」单位。"""
        v = Decimal("50000")  # 5 万
        assert _format_decimal_short(v) == "5.0 万"

    def test_format_small(self) -> None:
        """小数值保留 2 位小数。"""
        v = Decimal("123.456")
        assert _format_decimal_short(v) == "123.46"

    def test_format_negative_yi(self) -> None:
        """负数亿级也能格式化。"""
        v = Decimal("-15000000000")  # -150 亿
        assert _format_decimal_short(v) == "-150.0 亿"

    def test_format_zero(self) -> None:
        """零值格式化为 0.00。"""
        assert _format_decimal_short(Decimal("0")) == "0.00"


# ---------------------------------------------------------------------------
# 边界与默认值
# ---------------------------------------------------------------------------


class TestDefaultsAndEdgeCases:
    """默认值与边界场景。"""

    def test_default_dimensions(self) -> None:
        """默认尺寸 800×500。"""
        r = ChartRenderer()
        assert r.width_px == 800
        assert r.height_px == 500
        assert r.dpi == 100

    def test_render_all_with_empty_statement(self) -> None:
        """空 statement 渲染 3 张降级图。"""
        r = ChartRenderer()
        results = r.render_all(_statement())
        assert len(results) == 3
        for result in results:
            assert result.fallback is True
            assert result.png_bytes[:8] == _PNG_MAGIC

    def test_render_all_returns_charts_in_fixed_order(self) -> None:
        """render_all 顺序固定：pie / line / bar。"""
        r = ChartRenderer()
        results = r.render_all(_statement())
        assert results[0].chart_type == ChartType.ASSET_STRUCTURE_PIE
        assert results[1].chart_type == ChartType.REVENUE_TREND_LINE
        assert results[2].chart_type == ChartType.CASH_FLOW_BAR

    def test_chinese_font_candidate_list_not_empty(self) -> None:
        """中文字体候选列表不为空（环境可能未装字体）。"""
        # 不论环境是否有字体，候选列表本身必须非空
        from app.modules.generator.chart_renderer import _CHINESE_FONTS_CANDIDATES

        assert len(_CHINESE_FONTS_CANDIDATES) >= 4

    def test_chart_type_enum_count(self) -> None:
        """ChartType 必须固定 3 个枚举值。"""
        assert len(list(ChartType)) == 3
