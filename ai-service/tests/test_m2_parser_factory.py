"""Unit tests for DocumentParser factory wiring."""

from __future__ import annotations

from app.core.config import Settings
from app.modules.parser.document_parser import DocumentParser
from app.modules.parser.parser_factory import create_document_parser
from app.modules.parser.table_recognizer import TableRecognizer


def test_create_document_parser_without_paddle_providers() -> None:
    """Disabling Paddle keeps a lightweight parser suitable for MQ defaults."""
    parser = create_document_parser(
        Settings(),
        enable_table_recognition=False,
        enable_ocr=False,
    )

    assert isinstance(parser, DocumentParser)
    assert parser.layout_analyzer is None
    assert parser.ocr_provider is None


def test_create_document_parser_accepts_injected_analyzer() -> None:
    """Injected analyzers must bypass the default TableRecognizer."""
    analyzer = TableRecognizer(engine=lambda _: [])
    parser = create_document_parser(
        Settings(),
        enable_table_recognition=False,
        layout_analyzer=analyzer,
    )

    assert parser.layout_analyzer is analyzer
