"""Factory helpers for assembling the M2 DocumentParser stack."""

from __future__ import annotations

from app.core.config import Settings
from app.modules.parser.document_parser import (
    DocumentParser,
    LayoutAnalyzer,
    OcrProvider,
)
from app.modules.parser.ocr_fallback import OcrFallback
from app.modules.parser.table_recognizer import TableRecognizer


def create_document_parser(
    settings: Settings | None = None,
    *,
    enable_table_recognition: bool = True,
    enable_ocr: bool = True,
    layout_analyzer: LayoutAnalyzer | None = None,
    ocr_provider: OcrProvider | None = None,
) -> DocumentParser:
    """Build a DocumentParser with optional Paddle-backed providers.

    Production callers enable table/OCR providers by default. Unit tests can
    inject lightweight fakes through the ``layout_analyzer`` / ``ocr_provider``
    parameters or disable Paddle entirely.

    Args:
        settings: Application settings passed to DocumentParser.
        enable_table_recognition: When True and no analyzer is injected, attach
            a PP-Structure-backed TableRecognizer.
        enable_ocr: When True and no provider is injected, attach PaddleOCR
            fallback for scanned pages.
        layout_analyzer: Optional analyzer override for tests.
        ocr_provider: Optional OCR provider override for tests.

    Returns:
        A configured DocumentParser instance.
    """
    runtime_settings = settings or Settings()
    analyzer = layout_analyzer
    if analyzer is None and enable_table_recognition:
        analyzer = TableRecognizer(use_gpu=False)
    ocr = ocr_provider
    if ocr is None and enable_ocr:
        ocr = OcrFallback(use_gpu=False)
    return DocumentParser(
        settings=runtime_settings,
        layout_analyzer=analyzer,
        ocr_provider=ocr,
    )
