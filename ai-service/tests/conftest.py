"""Shared test fixtures for the M2 parser tests."""

from __future__ import annotations

import io
from pathlib import Path

import pytest


def _make_pdf(path: Path, pages: list[str], *, scanned: bool = False) -> bytes:
    """Build a small PDF in memory using PyMuPDF.

    Args:
        path: Unused destination kept for API symmetry.
        pages: One text string per desired page.
        scanned: When True, pages contain only a drawn rectangle (no text layer)
            so they emulate scanned pages that force OCR fallback.

    Returns:
        PDF bytes.
    """
    del path  # not needed; build in memory
    import fitz

    doc = fitz.open()
    for text in pages:
        page = doc.new_page(width=595, height=842)
        if scanned:
            page.draw_rect(fitz.Rect(40, 40, 555, 800), color=(0, 0, 0), width=1)
        else:
            page.insert_text((72, 72), text, fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


@pytest.fixture
def text_pdf_bytes() -> bytes:
    """A two-page text PDF for happy-path parser tests.

    Uses ASCII text to stay independent of CJK font availability in PyMuPDF;
    the parser is content-agnostic, so ASCII exercises the same code paths.
    """
    return _make_pdf(Path("unused"), ["Page One Title", "Cash 1234.56 RMB"])


@pytest.fixture
def scanned_pdf_bytes() -> bytes:
    """A single-page scan-like PDF with no extractable text layer."""
    return _make_pdf(Path("unused"), [""], scanned=True)


@pytest.fixture
def empty_pdf_bytes() -> bytes:
    """Invalid PDF bytes (header but zero pages) for the empty-document guard."""
    return b"%PDF-1.4\n%%EOF\n"
