"""M2.01 DocumentParser tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.exceptions import AiException
from app.modules.parser.document_parser import DocumentParser
from app.schemas.document import TableBlock, TextBlock, BoundingBox


class _FakeLayout:
    """Captures calls and returns a preset list of TableBlocks."""

    def __init__(
        self, tables: list[TableBlock] | None = None, raises: bool = False
    ) -> None:
        self.calls: list[tuple[int, bytes]] = []
        self.tables = tables or []
        self.raises = raises

    def analyze_page(self, page_index: int, image_bytes: bytes) -> list[TableBlock]:
        """Record the call and yield the preset tables (or raise)."""
        self.calls.append((page_index, image_bytes))
        if self.raises:
            raise RuntimeError("layout boom")
        return self.tables


class _FakeOcr:
    """Returns a preset list of OCR text blocks."""

    def __init__(self, blocks: list[TextBlock] | None = None) -> None:
        self.blocks = blocks or []
        self.calls: list[tuple[int, bytes]] = []

    def recognize(self, page_index: int, image_bytes: bytes) -> list[TextBlock]:
        """Record the call and yield the preset blocks."""
        self.calls.append((page_index, image_bytes))
        return self.blocks


def _bbox(x0: float = 0, y0: float = 0, x1: float = 10, y1: float = 10) -> BoundingBox:
    return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)


def test_parse_bytes_extracts_text_pages(text_pdf_bytes: bytes) -> None:
    """A text PDF produces two pages with non-empty text blocks."""
    parser = DocumentParser()
    document = parser.parse_bytes(text_pdf_bytes, source="uploads/text.pdf")

    assert document.page_count == 2
    assert document.source == "uploads/text.pdf"
    assert document.total_tables == 0
    assert not document.is_scanned
    page0 = document.pages[0]
    assert page0.page_index == 0
    assert page0.width > 0 and page0.height > 0
    assert any("Page One" in b.text for b in page0.text_blocks)


def test_parse_bytes_invokes_layout_analyzer(text_pdf_bytes: bytes) -> None:
    """A configured layout analyzer is called once per page with rendered bytes."""
    table = TableBlock(
        bbox=_bbox(5, 5, 100, 50), html="<table><tr><td>A</td></tr></table>"
    )
    layout = _FakeLayout(tables=[table])
    parser = DocumentParser(layout_analyzer=layout)

    document = parser.parse_bytes(text_pdf_bytes, source="uploads/text.pdf")

    assert len(layout.calls) == 2
    assert layout.calls[0][0] == 0
    assert layout.calls[0][1]  # non-empty PNG bytes
    assert document.total_tables == 2
    assert all(b.html == table.html for p in document.pages for b in p.table_blocks)


def test_layout_analyzer_failure_is_isolated(text_pdf_bytes: bytes) -> None:
    """A raising layout analyzer leaves the document parseable with empty tables."""
    layout = _FakeLayout(raises=True)
    parser = DocumentParser(layout_analyzer=layout)

    document = parser.parse_bytes(text_pdf_bytes, source="uploads/text.pdf")

    assert document.total_tables == 0
    assert document.page_count == 2


def test_scanned_page_triggers_ocr(scanned_pdf_bytes: bytes) -> None:
    """A page with no text layer is marked scanned and OCR is applied."""
    ocr_text = TextBlock(bbox=_bbox(0, 0, 80, 20), text="扫描识别文本")
    ocr = _FakeOcr(blocks=[ocr_text])
    parser = DocumentParser(ocr_provider=ocr)

    document = parser.parse_bytes(scanned_pdf_bytes, source="uploads/scan.pdf")

    assert document.is_scanned
    assert document.pages[0].is_scanned
    assert document.pages[0].ocr_applied
    assert ocr.calls, "OCR provider should have been invoked"
    assert any(b.text == "扫描识别文本" for b in document.pages[0].text_blocks)


def test_scanned_page_without_provider_stays_parsable(scanned_pdf_bytes: bytes) -> None:
    """Missing OCR provider still yields a Document, page flagged as scanned."""
    parser = DocumentParser()

    document = parser.parse_bytes(scanned_pdf_bytes, source="uploads/scan.pdf")

    assert document.is_scanned
    assert not document.pages[0].ocr_applied


def test_parse_bytes_rejects_empty_pdf(empty_pdf_bytes: bytes) -> None:
    """A header-only PDF that fitz cannot open raises AiException."""
    parser = DocumentParser()
    with pytest.raises(AiException, match="Failed to open PDF"):
        parser.parse_bytes(empty_pdf_bytes, source="uploads/empty.pdf")


def test_parse_bytes_rejects_garbage_input() -> None:
    """Non-PDF bytes raise AiException."""
    parser = DocumentParser()
    with pytest.raises(AiException, match="Failed to open PDF"):
        parser.parse_bytes(b"not a pdf", source="uploads/junk.pdf")


def test_fitz_import_error_raises_ai_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """When PyMuPDF cannot be imported, parsing surfaces AiException."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "fitz":
            raise ImportError("no fitz")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    parser = DocumentParser()
    with pytest.raises(AiException, match="PyMuPDF"):
        parser.parse_bytes(b"%PDF-1.4 junk", source="x.pdf")


def test_parse_pdf_file_reads_disk(tmp_path: Any, text_pdf_bytes: bytes) -> None:
    """parse_pdf_file opens a path and parses it."""
    from app.modules.parser.document_parser import parse_pdf_file

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(text_pdf_bytes)
    parser = DocumentParser()

    document = parse_pdf_file(parser, str(pdf_path), source="disk/sample.pdf")

    assert document.source == "disk/sample.pdf"
    assert document.page_count == 2


def test_to_text_block_skips_image_blocks() -> None:
    """_to_text_block returns None for image-only blocks."""
    parser = DocumentParser()
    image_block = {"type": 1, "bbox": (0, 0, 10, 10), "lines": []}
    assert parser._to_text_block(image_block) is None


def test_to_text_block_skips_empty_text() -> None:
    """_to_text_block returns None for blocks with only whitespace."""
    parser = DocumentParser()
    blank_block = {
        "type": 0,
        "bbox": (0, 0, 10, 10),
        "lines": [{"spans": [{"text": "   "}]}],
    }
    assert parser._to_text_block(blank_block) is None


def test_to_text_block_skips_zero_area_bbox() -> None:
    """A degenerate bbox is dropped."""
    parser = DocumentParser()
    degenerate = {
        "type": 0,
        "bbox": (5, 5, 5, 5),
        "lines": [{"spans": [{"text": "x"}]}],
    }
    assert parser._to_text_block(degenerate) is None


def test_metadata_carries_render_dpi(text_pdf_bytes: bytes) -> None:
    """Parser records the configured render DPI in document metadata."""
    parser = DocumentParser(render_dpi=150)
    document = parser.parse_bytes(text_pdf_bytes, source="x.pdf")
    assert document.metadata["render_dpi"] == 150
