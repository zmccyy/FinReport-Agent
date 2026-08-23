"""M4.04 extract handler 单测：MinIO 拉产物 + 表格筛选 + 契约 payload."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import AiException
from app.modules.extractor.handler import (
    configure_handler,
    extract_report_period,
    handle,
    reset_handler,
    select_table,
)
from app.modules.extractor.validator import Validator
from app.schemas.document import (
    BoundingBox,
    Document,
    Page,
    TableBlock,
    TextBlock,
)
from app.schemas.statement import (
    ExtractionResult,
    FinancialStatement,
    StatementItem,
    StatementType,
)
from app.schemas.task import TaskMessage

# ---------------------------------------------------------------------------
# Fake 基础设施
# ---------------------------------------------------------------------------

BS_HTML = (
    "<table><tr><td>货币资金</td><td>100</td></tr>"
    "<tr><td>资产总计</td><td>500</td></tr>"
    "<tr><td>负债合计</td><td>300</td></tr></table>"
)
CF_HTML = (
    "<table><tr><td>经营活动产生的现金流量净额</td><td>50</td></tr>"
    "<tr><td>筹资活动产生的现金流量</td><td>-20</td></tr></table>"
)


class FakeStore:
    """In-memory object store serving the parse artifact."""

    def __init__(self, artifact: bytes | None = None, error: Exception | None = None):
        self.artifact = artifact
        self.error = error
        self.requests: list[tuple[str, str | None]] = []

    def fetch_bytes(self, object_key: str, bucket: str | None = None) -> bytes:
        """Return the configured artifact bytes."""
        self.requests.append((object_key, bucket))
        if self.error is not None:
            raise self.error
        assert self.artifact is not None
        return self.artifact


def make_statement() -> FinancialStatement:
    """Build a valid BS FinancialStatement for the happy path."""
    return FinancialStatement(
        report_period="2024-12-31",
        currency="CNY",
        unit="元",
        statements={
            StatementType.BALANCE_SHEET: [
                StatementItem(item="货币资金", value=1.23e9),
                StatementItem(item="资产总计", value=5.67e10),
            ]
        },
    )


def make_result(
    statement: FinancialStatement | None = None, error: str | None = None
) -> ExtractionResult:
    """Build an ExtractionResult with deterministic token/latency numbers."""
    return ExtractionResult(
        statement_type=StatementType.BALANCE_SHEET,
        statement=statement,
        error=error,
        prompt_tokens=100,
        completion_tokens=50,
        latency_ms=1500.0,
    )


class FakeExtractor:
    """Scripted extractor duck-typing the Extractor contract."""

    def __init__(self, first: ExtractionResult, retry: ExtractionResult | None = None):
        self.first = first
        self.retry = retry
        self.extract_tables: list[str] = []
        self.retry_prompts: list[str] = []
        # 模拟 _CountingHub.calls：extract_with_retry 每轮 +1。
        self.hub = SimpleNamespace(calls=0)

    def extract(
        self, table_html: str, statement_type: StatementType, **kwargs: Any
    ) -> ExtractionResult:
        """Record the table and return the scripted first attempt."""
        self.extract_tables.append(table_html)
        self.hub.calls += 1
        return self.first

    def extract_with_prompt(
        self, prompt: str, statement_type: StatementType, **kwargs: Any
    ) -> ExtractionResult:
        """Record the retry prompt and return the scripted retry attempt."""
        self.retry_prompts.append(prompt)
        self.hub.calls += 1
        assert self.retry is not None
        return self.retry


def make_document(
    *, with_bs: bool = True, with_cf: bool = True, title: str | None = "2024年年度报告"
) -> Document:
    """Build a two-page Document with optional BS/CF tables and a cover title."""
    page0_blocks: list[Any] = []
    if title:
        page0_blocks.append(TextBlock(bbox=BoundingBox(x0=0, y0=0, x1=100, y1=10), text=title))
    if with_cf:
        # M4.10：报表段由「合并现金流量表」标题定位（页文本块）。
        page0_blocks.append(
            TextBlock(bbox=BoundingBox(x0=0, y0=12, x1=200, y1=20), text="合并现金流量表")
        )
    page0_tables = (
        [TableBlock(bbox=BoundingBox(x0=0, y0=20, x1=100, y1=80), html=CF_HTML)] if with_cf else []
    )
    page1_blocks: list[Any] = []
    if with_bs:
        page1_blocks.append(
            TextBlock(bbox=BoundingBox(x0=0, y0=12, x1=200, y1=20), text="合并资产负债表")
        )
    page1_tables = (
        [TableBlock(bbox=BoundingBox(x0=0, y0=120, x1=100, y1=200), html=BS_HTML)]
        if with_bs
        else []
    )
    return Document(
        source="uploads/1/demo.pdf",
        page_count=2,
        pages=[
            Page(
                page_index=0,
                width=595,
                height=842,
                text_blocks=page0_blocks,
                table_blocks=page0_tables,
            ),
            Page(
                page_index=1,
                width=595,
                height=842,
                text_blocks=page1_blocks,
                table_blocks=page1_tables,
            ),
        ],
    )


def build_message(step: str = "extract.bs", task_id: str = "task-x1") -> TaskMessage:
    """Build a valid extract TaskMessage."""
    return TaskMessage(
        taskId=task_id,
        step=step,
        payload={},
        idempotencyKey=f"{task_id}:{step}",
    )


@pytest.fixture(autouse=True)
def _reset_extract_handler() -> None:
    """Keep handler tests isolated from singleton state."""
    reset_handler()
    yield
    reset_handler()


# ---------------------------------------------------------------------------
# select_table / extract_report_period 纯函数
# ---------------------------------------------------------------------------


def test_select_table_picks_highest_scoring_table() -> None:
    """BS step must pick the BS table (page 1), not the CF table (page 0)."""
    document = make_document()
    selected = select_table(document, StatementType.BALANCE_SHEET)
    assert selected is not None
    pages, merged_html, scope = selected
    assert pages == [1]
    assert "资产总计" in merged_html
    assert scope == "合并"


def test_select_table_returns_none_without_match() -> None:
    """A document without matching keywords yields None for that type."""
    document = make_document(with_bs=False, with_cf=True, title=None)
    assert select_table(document, StatementType.CASH_FLOW) is not None
    assert select_table(document, StatementType.BALANCE_SHEET) is None


def test_select_table_prefers_consolidated_over_parent_company() -> None:
    """同分时合并报表优先（页级母公司标题强降权 -2）。"""
    parent_html = BS_HTML + "<tr><td>母公司</td><td>x</td></tr>"
    document = Document(
        source="uploads/1/demo.pdf",
        page_count=2,
        pages=[
            Page(
                page_index=0,
                width=595,
                height=842,
                text_blocks=[
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=0, x1=200, y1=10),
                        text="母公司资产负债表",
                    )
                ],
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=20, x1=100, y1=100), html=parent_html)
                ],
            ),
            Page(
                page_index=1,
                width=595,
                height=842,
                text_blocks=[
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=110, x1=200, y1=120),
                        text="合并资产负债表",
                    )
                ],
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=120, x1=100, y1=200), html=BS_HTML)
                ],
            ),
        ],
    )
    selected = select_table(document, StatementType.BALANCE_SHEET)
    assert selected is not None
    pages, merged_html, scope = selected
    assert pages == [1]
    assert "母公司" not in merged_html
    assert scope == "合并"


def test_select_table_merges_tables_across_pages() -> None:
    """M4.10：合并利润表跨页拆散时，命中表全部拼接交给 LLM。"""
    is_page1 = (
        "<table><tr><td>其中：营业收入</td><td>1688</td></tr>"
        "<tr><td>其中：营业成本</td><td>148</td></tr></table>"
    )
    is_page2 = "<table><tr><td>五、净利润</td><td>853</td></tr></table>"
    parent_is = (
        "<table><tr><td>一、营业收入</td><td>983</td></tr>"
        "<tr><td>四、净利润</td><td>853</td></tr>"
        "<tr><td>营业成本</td><td>160</td></tr>"
        "<tr><td>利润总额</td><td>976</td></tr></table>"
    )
    document = Document(
        source="uploads/1/demo.pdf",
        page_count=3,
        pages=[
            Page(
                page_index=0, width=595, height=842,
                text_blocks=[
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=0, x1=200, y1=10),
                        text="合并利润表",
                    )
                ],
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=20, x1=100, y1=100), html=is_page1)
                ],
            ),
            Page(
                page_index=1, width=595, height=842,
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=0, x1=100, y1=100), html=is_page2)
                ],
            ),
            Page(
                page_index=2, width=595, height=842,
                text_blocks=[
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=0, x1=200, y1=10),
                        text="母公司利润表",
                    )
                ],
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=20, x1=100, y1=100), html=parent_is)
                ],
            ),
        ],
    )
    selected = select_table(document, StatementType.INCOME_STATEMENT)
    assert selected is not None
    pages, merged_html, scope = selected
    assert pages == [0, 1]  # 母公司页被排除，合并表两页都拼接
    assert "营业收入" in merged_html and "净利润" in merged_html
    assert "母公司" not in merged_html
    assert scope == "合并"


def test_select_table_uses_text_fallback_for_tableless_page() -> None:
    """M4.10：页命中关键词但无表格（PP 漏检）时用文本行重建 HTML。"""
    document = Document(
        source="uploads/1/demo.pdf",
        page_count=1,
        pages=[
            Page(
                page_index=0,
                width=595,
                height=842,
                text_blocks=[
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=0, x1=200, y1=10),
                        text="合并现金流量表",
                    ),
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=20, x1=300, y1=30),
                        text="销售商品、提供劳务收到的现金 183,990,403,487.80",
                    ),
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=32, x1=300, y1=42),
                        text="经营活动产生的现金流量净额 61,522,204,989.35",
                    ),
                ],
            ),
        ],
    )
    selected = select_table(document, StatementType.CASH_FLOW)
    assert selected is not None
    pages, merged_html, scope = selected
    assert pages == [0]
    assert "销售商品、提供劳务收到的现金" in merged_html
    assert "183990403487.80" in merged_html
    assert scope == "合并"


def test_select_table_cuts_parent_tables_on_same_page() -> None:
    """M3（审查修复）：合并/母公司标题同页时，母公司表不得以合并口径并入。

    旧版 start 分支（仅取合并标题下方全部表）遮蔽 end 分支的 y 切分，
    母公司标题下方的母公司表会被并入合并段。
    """
    merged_is = (
        "<table><tr><td>营业收入</td><td>1688</td></tr>"
        "<tr><td>净利润</td><td>853</td></tr></table>"
    )
    parent_is = "<table><tr><td>母公司营业收入</td><td>983</td></tr></table>"
    document = Document(
        source="uploads/1/demo.pdf",
        page_count=1,
        pages=[
            Page(
                page_index=0,
                width=595,
                height=842,
                text_blocks=[
                    TextBlock(bbox=BoundingBox(x0=0, y0=0, x1=200, y1=10), text="合并利润表"),
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=100, x1=200, y1=110), text="母公司利润表"
                    ),
                ],
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=20, x1=100, y1=90), html=merged_is),
                    TableBlock(
                        bbox=BoundingBox(x0=0, y0=120, x1=100, y1=180), html=parent_is
                    ),
                ],
            ),
        ],
    )
    selected = select_table(document, StatementType.INCOME_STATEMENT)
    assert selected is not None
    pages, merged_html, scope = selected
    assert pages == [0]
    assert "营业收入" in merged_html
    assert "母公司营业收入" not in merged_html
    assert scope == "合并"


def test_select_table_implicit_boundary_when_parent_title_missing() -> None:
    """H1（审查修复）：母公司标题缺失时，下一张报表标题作为隐式段边界。

    旧版 end_page=None 会把起始页之后到文档末尾的全部表格（母公司表、
    附注表）并入合并段。
    """
    bs_html = (
        "<table><tr><td>货币资金</td><td>100</td></tr>"
        "<tr><td>资产总计</td><td>500</td></tr></table>"
    )
    bs_tail = "<table><tr><td>负债合计</td><td>300</td></tr></table>"
    is_html = "<table><tr><td>营业收入</td><td>1688</td></tr></table>"
    notes_html = "<table><tr><td>附注说明</td><td>x</td></tr></table>"
    document = Document(
        source="uploads/1/demo.pdf",
        page_count=4,
        pages=[
            Page(
                page_index=0,
                width=595,
                height=842,
                text_blocks=[
                    TextBlock(bbox=BoundingBox(x0=0, y0=0, x1=200, y1=10), text="合并资产负债表")
                ],
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=20, x1=100, y1=90), html=bs_html)
                ],
            ),
            Page(
                page_index=1,
                width=595,
                height=842,
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=0, x1=100, y1=90), html=bs_tail)
                ],
            ),
            Page(
                page_index=2,
                width=595,
                height=842,
                text_blocks=[
                    TextBlock(bbox=BoundingBox(x0=0, y0=0, x1=200, y1=10), text="合并利润表")
                ],
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=20, x1=100, y1=90), html=is_html)
                ],
            ),
            Page(
                page_index=3,
                width=595,
                height=842,
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=0, x1=100, y1=90), html=notes_html)
                ],
            ),
        ],
    )
    selected = select_table(document, StatementType.BALANCE_SHEET)
    assert selected is not None
    pages, merged_html, scope = selected
    # 段在「合并利润表」页截断：起始页 + 续页，利润表/附注页排除。
    assert pages == [0, 1]
    assert "资产总计" in merged_html and "负债合计" in merged_html
    assert "营业收入" not in merged_html
    assert "附注说明" not in merged_html
    assert scope == "合并"


def test_select_table_density_fallback_when_no_end_titles() -> None:
    """H1（审查修复）：六类标题均缺失时按关键词密度收窄，远端附注页不并入。

    顺序扫描间隔 >1 页无命中即停止——后续附注页即使含关键词（如
    「经营活动产生的现金流量」的附注讨论）也不得扩展段尾。
    """
    cf_head = "<table><tr><td>经营活动产生的现金流量净额</td><td>61</td></tr></table>"
    cf_tail = "<table><tr><td>现金及现金等价物净增加额</td><td>52</td></tr></table>"
    notes_far = "<table><tr><td>经营活动产生的现金流量附注</td><td>x</td></tr></table>"
    document = Document(
        source="uploads/1/demo.pdf",
        page_count=4,
        pages=[
            Page(
                page_index=0,
                width=595,
                height=842,
                text_blocks=[
                    TextBlock(bbox=BoundingBox(x0=0, y0=0, x1=200, y1=10), text="合并现金流量表")
                ],
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=20, x1=100, y1=90), html=cf_head)
                ],
            ),
            Page(
                page_index=1,
                width=595,
                height=842,
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=0, x1=100, y1=90), html=cf_tail)
                ],
            ),
            # page2：间隔页（无关键词、无表格）。
            Page(page_index=2, width=595, height=842),
            # page3：远端附注页，文本含 CF 关键词——不得扩展段尾。
            Page(
                page_index=3,
                width=595,
                height=842,
                text_blocks=[
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=0, x1=300, y1=10),
                        text="附注：经营活动产生的现金流量说明",
                    )
                ],
                table_blocks=[
                    TableBlock(bbox=BoundingBox(x0=0, y0=20, x1=100, y1=90), html=notes_far)
                ],
            ),
        ],
    )
    selected = select_table(document, StatementType.CASH_FLOW)
    assert selected is not None
    pages, merged_html, scope = selected
    assert pages == [0, 1]
    assert "经营活动产生的现金流量净额" in merged_html
    assert "现金及现金等价物净增加额" in merged_html
    assert "附注" not in merged_html
    assert scope == "合并"


def test_fallback_rebuild_excludes_above_title_and_glued_notes() -> None:
    """M1（审查修复）：降级重建的标题边界 / 附注号粘值 / 熔合行防护。

    - 标题上方上一报表尾块（利润表尾「综合收益总额」）不进入重建；
    - 「资产处置收益53553,106,625.19」类粘值行取真实金额 553,106,625.19
      （严格千分位分组跳过非法前导组「53553」），残留附注号被剥离；
    - 多名称熔合长块（>30 字符）直接丢弃，宁缺勿错。
    """
    document = Document(
        source="uploads/1/demo.pdf",
        page_count=1,
        pages=[
            Page(
                page_index=0,
                width=595,
                height=842,
                text_blocks=[
                    # 利润表尾块（位于 CF 标题上方，应被 min_y0 排除）。
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=50, x1=300, y1=60),
                        text="六、综合收益总额85,305,341,965.73",
                    ),
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=100, x1=200, y1=110),
                        text="合并现金流量表",
                    ),
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=110, x1=300, y1=120),
                        text="销售商品、提供劳务收到的现金 183,990,403,487.80",
                    ),
                    # 附注号 53 与金额粘连：真实金额 553,106,625.19。
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=130, x1=300, y1=140),
                        text="资产处置收益53553,106,625.19",
                    ),
                    # 多名称熔合块（~40 字符）：应被长度上限丢弃。
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=150, x1=400, y1=160),
                        text=(
                            "收到再保业务现金净额保户储金及投资款净增加额"
                            "收取利息、手续费及佣金的现金2,525,372,050.12"
                        ),
                    ),
                    TextBlock(
                        bbox=BoundingBox(x0=0, y0=170, x1=300, y1=180),
                        text="经营活动产生的现金流量净额 61,522,204,989.35",
                    ),
                ],
            ),
        ],
    )
    selected = select_table(document, StatementType.CASH_FLOW)
    assert selected is not None
    _, merged_html, _ = selected
    # 上一报表尾块被排除。
    assert "综合收益总额" not in merged_html
    # 正常行保留。
    assert "销售商品、提供劳务收到的现金" in merged_html
    assert "183990403487.80" in merged_html
    assert "61522204989.35" in merged_html
    # 粘值行：取真实金额并剥残留附注号。
    assert "资产处置收益" in merged_html
    assert "553106625.19" in merged_html
    assert "53553106625.19" not in merged_html
    # 熔合长块丢弃。
    assert "收到再保" not in merged_html
    assert "2525372050.12" not in merged_html


def test_extract_report_period_variants() -> None:
    """Cover title maps to the A-share report end date."""
    assert extract_report_period(make_document(title="2024年年度报告")) == "2024-12-31"
    assert extract_report_period(make_document(title="2025年第一季度报告")) == "2025-03-31"
    assert extract_report_period(make_document(title="2024年半年度报告")) == "2024-06-30"
    assert extract_report_period(make_document(title="2024年第三季度报告")) == "2024-09-30"
    assert extract_report_period(make_document(title="公司公告")) == ""


# ---------------------------------------------------------------------------
# handle 契约
# ---------------------------------------------------------------------------


def test_handle_returns_m209_contract_payload() -> None:
    """Happy path: payload matches the M2.09 shape consumed by StatementWriter."""
    artifact = make_document().model_dump_json().encode("utf-8")
    store = FakeStore(artifact)
    extractor = FakeExtractor(first=make_result(make_statement()))
    configure_handler(extractor=extractor, object_store=store)

    payload = asyncio.run(handle(build_message()))

    assert store.requests == [("parsed/task-x1.json", "finreport-artifacts")]
    assert extractor.extract_tables == [BS_HTML]
    assert payload["success"] is True
    assert payload["statement"]["report_period"] == "2024-12-31"
    assert payload["statement"]["statements"]["balance_sheet"][0]["item"] == "货币资金"
    assert payload["validation"]["is_valid"] is True
    assert 0.5 <= payload["confidence"] <= 1.0
    assert payload["source_page"] == 1
    assert payload["retried"] is False
    assert payload["tokens_used"] == 150
    assert payload["latency_ms"] == 1500.0


def test_handle_rejects_unknown_step() -> None:
    """Unknown routing steps must raise instead of writing fake data."""
    configure_handler(
        extractor=FakeExtractor(first=make_result(make_statement())),
        object_store=FakeStore(b"{}"),
    )
    with pytest.raises(ValueError, match="Unknown extract step"):
        asyncio.run(handle(build_message(step="extract.xyz")))


def test_handle_raises_when_artifact_fetch_fails() -> None:
    """A missing parse artifact must surface as AiException (DLQ path)."""
    store = FakeStore(error=AiException("MinIO fetch failed"))
    configure_handler(
        extractor=FakeExtractor(first=make_result(make_statement())),
        object_store=store,
    )
    with pytest.raises(AiException, match="MinIO fetch failed"):
        asyncio.run(handle(build_message()))


def test_handle_raises_when_artifact_schema_mismatches() -> None:
    """Garbage artifact bytes must raise AiException, not crash on parse."""
    configure_handler(
        extractor=FakeExtractor(first=make_result(make_statement())),
        object_store=FakeStore(b"not-a-document"),
    )
    with pytest.raises(AiException, match="schema mismatch"):
        asyncio.run(handle(build_message()))


def test_handle_raises_when_no_matching_table() -> None:
    """No BS table in the artifact is a permanent failure → AiException."""
    artifact = make_document(with_bs=False).model_dump_json().encode("utf-8")
    configure_handler(
        extractor=FakeExtractor(first=make_result(make_statement())),
        object_store=FakeStore(artifact),
    )
    with pytest.raises(AiException, match="no balance_sheet table"):
        asyncio.run(handle(build_message()))


def test_handle_marks_retried_when_retry_succeeds() -> None:
    """First attempt fails validation → retry succeeds → retried=True."""
    artifact = make_document().model_dump_json().encode("utf-8")
    extractor = FakeExtractor(
        first=make_result(error="no JSON object found in model output"),
        retry=make_result(make_statement()),
    )
    configure_handler(extractor=extractor, object_store=FakeStore(artifact))

    payload = asyncio.run(handle(build_message()))

    assert len(extractor.extract_tables) == 1
    assert len(extractor.retry_prompts) == 1
    assert payload["retried"] is True
    assert payload["success"] is True


def test_handle_payload_uses_real_validator_rules() -> None:
    """The real Validator flags a bad report_period as an error issue."""
    bad = FinancialStatement(
        report_period="2024/12/31",
        statements={StatementType.BALANCE_SHEET: [StatementItem(item="货币资金", value=1.0)]},
    )
    artifact = make_document().model_dump_json().encode("utf-8")
    # 首抽与重试都返回坏 period：extract_with_retry 走完重试链后仍 invalid。
    configure_handler(
        extractor=FakeExtractor(first=make_result(bad), retry=make_result(bad)),
        validator=Validator(),
        object_store=FakeStore(artifact),
    )

    payload = asyncio.run(handle(build_message()))

    assert payload["validation"]["is_valid"] is False
    codes = [i["code"] for i in payload["validation"]["issues"]]
    assert "invalid_report_period" in codes
    assert payload["confidence"] < 1.0


def test_handle_statement_json_is_round_trippable() -> None:
    """The serialized statement block must be plain JSON (enum keys → strings)."""
    artifact = make_document().model_dump_json().encode("utf-8")
    configure_handler(
        extractor=FakeExtractor(first=make_result(make_statement())),
        object_store=FakeStore(artifact),
    )

    payload = asyncio.run(handle(build_message()))
    decoded = json.loads(json.dumps(payload["statement"]))
    assert decoded["statements"]["balance_sheet"][0]["scope"] == "合并"


def test_counting_hub_injects_json_mode_and_counts_calls() -> None:
    """The production hub adapter forces json_mode (AGENTS.md §8.1) and counts."""
    from app.modules.extractor.handler import _CountingHub

    inner = SimpleNamespace(settings=SimpleNamespace(), calls=[])
    inner.generate = lambda prompt, **kw: inner.calls.append((prompt, kw))

    adapter = _CountingHub(inner)
    adapter.generate("p1")
    adapter.generate("p2", json_mode=False)

    assert adapter.calls == 2
    assert inner.calls[0][1]["json_mode"] is True
    # 显式传入的 json_mode 不被覆盖（setdefault 语义）。
    assert inner.calls[1][1]["json_mode"] is False
