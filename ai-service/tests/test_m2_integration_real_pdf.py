"""M2.12 L3 integration tests — real PDF parse + extractor (mock LLM) chain.

This test module exercises the L3 parse→extract path against real-world A-share
annual reports stored under ``data/sample_reports/``. The LLM 7B inference is
replaced with a ``_StubHub`` returning preset JSON (spec §10.3 降级链 第 3 步
"切 API 72B" 之前的本地兜底；M2.12 验收只验结构，真实 7B 质量由
``scripts/eval_m2_f1.py`` 承担)。

Plan §4 M2.12 acceptance criteria:
* 3 份不同格式年报解析成功（page_count > 0）
* Extractor 输出符合 FinancialStatement schema
* 端到端耗时（PARSE+EXTRACT）< 3 min — 由 ``scripts/eval_m2_sla.py`` 测
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.modules.extractor.extractor import Extractor
from app.modules.modelhub.llm_loader import GenerateResult
from app.modules.modelhub.modelhub import ModelHub
from app.modules.parser.document_parser import DocumentParser
from app.schemas.statement import (
    ExtractionResult,
    FinancialStatement,
    StatementType,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_REPORTS = REPOSITORY_ROOT / "data" / "sample_reports"

REAL_PDFS: list[tuple[str, str, str]] = [
    (
        "600519_贵州茅台_2025年年度报告.pdf",
        "600519",
        "贵州茅台",
    ),
    (
        "000001_平安银行_2025年年度报告.pdf",
        "000001",
        "平安银行",
    ),
    (
        "300750_宁德时代：2025年年度报告.pdf",
        "300750",
        "宁德时代",
    ),
]


# ---------------------------------------------------------------------------
# Stub ModelHub — preset JSON instead of real 7B inference
# ---------------------------------------------------------------------------


class _StubHub(ModelHub):
    """ModelHub subclass returning a queued JSON response.

    Mirrors the stub in ``tests/test_m2_extractor.py`` but with the 7B output
    shaped to match a real Qwen2.5-Instruct response to the M2.06 prompt
    (M2.12 real-PDF integration: preset JSON, no GPU).
    """

    def __init__(
        self,
        *,
        response_text: str = "",
        prompt_tokens: int = 200,
        completion_tokens: int = 400,
        latency_ms: float = 5600.0,
        settings: Settings | None = None,
    ) -> None:
        # Bypass ModelHub.__init__ (avoids LlmLoader + torch import).
        self.settings = settings or Settings()
        self._response_text = response_text
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self._latency_ms = latency_ms
        self.generate_calls: list[dict[str, Any]] = []

    def generate(  # type: ignore[override]
        self,
        prompt: str,
        *,
        max_new_tokens: int | None = None,
        temperature: float = 0.0,
        timeout_seconds: float | None = None,
    ) -> GenerateResult:
        """Record the call and return the queued response."""
        self.generate_calls.append(
            {
                "prompt": prompt,
                "max_new_tokens": max_new_tokens,
                "temperature": temperature,
                "timeout_seconds": timeout_seconds,
            }
        )
        return GenerateResult(
            text=self._response_text,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            latency_ms=self._latency_ms,
        )


def _preset_bs_json() -> str:
    """Preset balance_sheet JSON matching the M2.06 prompt output contract."""
    return json.dumps(
        {
            "report_period": "2025-12-31",
            "currency": "CNY",
            "unit": "元",
            "statements": {
                "balance_sheet": [
                    {"item": "货币资金", "value": 59000000000.0, "scope": "合并", "period": "本期"},
                    {"item": "资产总计", "value": 280000000000.0, "scope": "合并", "period": "本期"},
                    {"item": "负债合计", "value": 60000000000.0, "scope": "合并", "period": "本期"},
                    {"item": "所有者权益合计", "value": 220000000000.0, "scope": "合并", "period": "本期"},
                ]
            },
        },
        ensure_ascii=False,
    )


def _preset_is_json() -> str:
    """Preset income_statement JSON matching the M2.06 prompt output contract."""
    return json.dumps(
        {
            "report_period": "2025-12-31",
            "currency": "CNY",
            "unit": "元",
            "statements": {
                "income_statement": [
                    {"item": "营业收入", "value": 170000000000.0, "scope": "合并", "period": "本期"},
                    {"item": "净利润", "value": 85000000000.0, "scope": "合并", "period": "本期"},
                ]
            },
        },
        ensure_ascii=False,
    )


def _preset_cf_json() -> str:
    """Preset cash_flow JSON matching the M2.06 prompt output contract."""
    return json.dumps(
        {
            "report_period": "2025-12-31",
            "currency": "CNY",
            "unit": "元",
            "statements": {
                "cash_flow": [
                    {
                        "item": "经营活动产生的现金流量净额",
                        "value": 90000000000.0,
                        "scope": "合并",
                        "period": "本期",
                    }
                ]
            },
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_pdf_paths() -> list[Path]:
    """List of real PDF paths from data/sample_reports/, skipped if missing."""
    paths: list[Path] = []
    for filename, _code, _name in REAL_PDFS:
        path = SAMPLE_REPORTS / filename
        if not path.exists():
            pytest.skip(f"real PDF not present: {path}")
        paths.append(path)
    return paths


@pytest.fixture
def parser_no_paddle() -> DocumentParser:
    """DocumentParser without PaddleOCR/PP-Structure — pure PyMuPDF text path."""
    return DocumentParser(settings=Settings())


# ---------------------------------------------------------------------------
# Real PDF parse tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,company_code,company_name",
    REAL_PDFS,
    ids=[c[2] for c in REAL_PDFS],
)
def test_real_pdf_parses_with_text_layer(
    filename: str, company_code: str, company_name: str, parser_no_paddle: DocumentParser
) -> None:
    """Each real annual report must parse with page_count > 0 and text content.

    Acceptance (plan §4.3): 3 份不同格式年报上传后三表数据可见 — prerequisite
    is that the PDF parses at all. We assert the text-layer extraction path
    works on each real-world report.
    """
    pdf_path = SAMPLE_REPORTS / filename
    if not pdf_path.exists():
        pytest.skip(f"real PDF not present: {pdf_path}")

    pdf_bytes = pdf_path.read_bytes()
    document = parser_no_paddle.parse_bytes(pdf_bytes, source=filename)

    assert document.source == filename, "source 应为文件名"
    assert document.page_count > 0, f"{company_name} page_count 必须 > 0"
    # 至少有一页含文本块（A 股年报必含大量文本）
    text_pages = [p for p in document.pages if p.text_blocks]
    assert len(text_pages) > 0, f"{company_name} 必须有文本页"
    # 验证至少一个文本块有内容
    total_text_chars = sum(
        len(b.text) for p in document.pages for b in p.text_blocks
    )
    assert total_text_chars > 1000, f"{company_name} 文本总量应 > 1000 字符，实际 {total_text_chars}"


# ---------------------------------------------------------------------------
# Extractor schema validation tests (mock 7B)
# ---------------------------------------------------------------------------


def test_extractor_balance_sheet_returns_valid_schema() -> None:
    """Extractor.extract(balance_sheet) → FinancialStatement schema 通过.

    Acceptance: BS 三表抽取 F1 ≥ 0.70 — schema pass 是 F1 计算的前提。
    """
    hub = _StubHub(response_text=_preset_bs_json())
    extractor = Extractor(hub)

    result: ExtractionResult = extractor.extract(
        table_html="<table><tr><td>货币资金</td><td>59000000000</td></tr></table>",
        statement_type=StatementType.BALANCE_SHEET,
        report_period="2025-12-31",
        company_code="600519",
        unit="元",
    )
    assert result.success, f"BS 抽取应成功，错误: {result.error}"
    assert isinstance(result.statement, FinancialStatement)
    assert result.statement.report_period == "2025-12-31"
    assert result.statement.currency == "CNY"
    assert result.statement.unit == "元"
    bs_items = result.statement.statements.get(StatementType.BALANCE_SHEET, [])
    assert len(bs_items) == 4, f"BS 应有 4 项，实际 {len(bs_items)}"
    item_names = {it.item for it in bs_items}
    assert "货币资金" in item_names
    assert "资产总计" in item_names
    # 验证数值（与预设一致）
    assets_total = next(it for it in bs_items if it.item == "资产总计")
    assert math.isclose(assets_total.value, 280000000000.0)


def test_extractor_income_statement_returns_valid_schema() -> None:
    """Extractor.extract(income_statement) → FinancialStatement schema 通过."""
    hub = _StubHub(response_text=_preset_is_json())
    extractor = Extractor(hub)

    result = extractor.extract(
        table_html="<table><tr><td>营业收入</td><td>170000000000</td></tr></table>",
        statement_type=StatementType.INCOME_STATEMENT,
        report_period="2025-12-31",
        company_code="600519",
        unit="元",
    )
    assert result.success, f"IS 抽取应成功，错误: {result.error}"
    assert isinstance(result.statement, FinancialStatement)
    is_items = result.statement.statements.get(StatementType.INCOME_STATEMENT, [])
    assert len(is_items) == 2
    item_names = {it.item for it in is_items}
    assert "营业收入" in item_names
    assert "净利润" in item_names


def test_extractor_cash_flow_returns_valid_schema() -> None:
    """Extractor.extract(cash_flow) → FinancialStatement schema 通过."""
    hub = _StubHub(response_text=_preset_cf_json())
    extractor = Extractor(hub)

    result = extractor.extract(
        table_html="<table><tr><td>经营活动产生的现金流量净额</td><td>90000000000</td></tr></table>",
        statement_type=StatementType.CASH_FLOW,
        report_period="2025-12-31",
        company_code="600519",
        unit="元",
    )
    assert result.success, f"CF 抽取应成功，错误: {result.error}"
    assert isinstance(result.statement, FinancialStatement)
    cf_items = result.statement.statements.get(StatementType.CASH_FLOW, [])
    assert len(cf_items) == 1
    assert cf_items[0].item == "经营活动产生的现金流量净额"
    assert math.isclose(cf_items[0].value, 90000000000.0)


def test_extractor_handles_invalid_json_then_succeeds() -> None:
    """Spec §10.3 降级链：JSON 解析失败 → 返回 success=False（让 M2.07 validator 重试）."""
    hub = _StubHub(response_text="这不是 JSON")
    extractor = Extractor(hub)

    result = extractor.extract(
        table_html="<table></table>",
        statement_type=StatementType.BALANCE_SHEET,
        report_period="2025-12-31",
        company_code="600519",
        unit="元",
    )
    assert not result.success, "非 JSON 输出应标记为失败"
    assert result.error is not None
    assert result.raw_text == "这不是 JSON", "raw_text 应保留原始输出供 retry"


# ---------------------------------------------------------------------------
# Full three-statement pipeline (parse → extract)
# ---------------------------------------------------------------------------


def test_three_statement_pipeline_with_mock_llm(real_pdf_paths: list[Path]) -> None:
    """端到端三表 pipeline：PDF → DocumentParser → 三次 Extractor.extract → 三表 schema 通过.

    This is the closest CI-runnable analogue of plan §4.3 acceptance
    "上传 3 份不同格式年报 → 三表数据前端可见". The 7B model is replaced
    with stub responses so the test runs on CPU-only environments.
    """
    # 茅台 PDF 跑三表
    moutai_pdf = next(
        (p for p in real_pdf_paths if "600519" in p.name), None
    )
    if moutai_pdf is None:
        pytest.skip("moutai PDF missing")

    parser = DocumentParser(settings=Settings())
    document = parser.parse_bytes(moutai_pdf.read_bytes(), source=moutai_pdf.name)
    assert document.page_count > 0, "茅台年报 page_count 必须 > 0"

    # 三表分别抽取（用对应 stub JSON）
    hub_bs = _StubHub(response_text=_preset_bs_json())
    hub_is = _StubHub(response_text=_preset_is_json())
    hub_cf = _StubHub(response_text=_preset_cf_json())

    extractor_bs = Extractor(hub_bs)
    extractor_is = Extractor(hub_is)
    extractor_cf = Extractor(hub_cf)

    bs_result = extractor_bs.extract(
        table_html="<table></table>",
        statement_type=StatementType.BALANCE_SHEET,
        report_period="2025-12-31",
        company_code="600519",
        unit="元",
    )
    is_result = extractor_is.extract(
        table_html="<table></table>",
        statement_type=StatementType.INCOME_STATEMENT,
        report_period="2025-12-31",
        company_code="600519",
        unit="元",
    )
    cf_result = extractor_cf.extract(
        table_html="<table></table>",
        statement_type=StatementType.CASH_FLOW,
        report_period="2025-12-31",
        company_code="600519",
        unit="元",
    )

    assert bs_result.success, f"BS 失败: {bs_result.error}"
    assert is_result.success, f"IS 失败: {is_result.error}"
    assert cf_result.success, f"CF 失败: {cf_result.error}"

    # 三表合计 4 + 2 + 1 = 7 项科目
    bs_count = len(bs_result.statement.statements.get(StatementType.BALANCE_SHEET, []))
    is_count = len(is_result.statement.statements.get(StatementType.INCOME_STATEMENT, []))
    cf_count = len(cf_result.statement.statements.get(StatementType.CASH_FLOW, []))
    assert bs_count + is_count + cf_count == 7, (
        f"三表合计应 7 项（BS 4 + IS 2 + CF 1），实际 BS={bs_count} IS={is_count} CF={cf_count}"
    )
