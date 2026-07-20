"""M2.01 HTTP /parse/upload endpoint tests."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.api.parse import get_document_parser
from app.core.config import Settings
from app.main import create_app
from app.modules.parser.document_parser import DocumentParser


def _two_page_pdf() -> bytes:
    """Build a small two-page ASCII PDF."""
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Alpha page", fontsize=12)
    doc.new_page().insert_text((72, 72), "Beta page", fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def test_parse_upload_returns_real_document() -> None:
    """Uploading a PDF returns a real parsed Document summary."""
    app = create_app(Settings(mq_consumer_enabled=False))
    client = TestClient(app)

    pdf = _two_page_pdf()
    response = client.post(
        "/parse/upload",
        files={"file": ("sample.pdf", pdf, "application/pdf")},
    )

    assert response.status_code == 200
    body = response.json()["document"]
    assert body["page_count"] == 2
    assert body["source"] == "sample.pdf"
    assert body["extra"]["parser_version"] == "m6-v1"
    assert body["extra"]["page_count"] == 2


def test_parse_upload_rejects_empty_file() -> None:
    """An empty upload is rejected with a 500 containing AiException text."""
    app = create_app(Settings(mq_consumer_enabled=False))
    client = TestClient(app)

    response = client.post(
        "/parse/upload",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )

    assert response.status_code == 500
    assert "empty" in response.json()["detail"].lower()


def test_parse_upload_uses_overridden_parser() -> None:
    """Tests can swap the parser via dependency_overrides (function form)."""
    from app.schemas.document import Document as _Doc

    app = create_app(Settings(mq_consumer_enabled=False))

    sentinel_page_count = 7

    class _StubParser(DocumentParser):
        def parse_bytes(self, pdf_bytes: bytes, source: str):  # type: ignore[override]
            """Return a tiny stub Document."""
            return _Doc(
                source=source,
                page_count=sentinel_page_count,
                pages=[],
                parser_version="stub-v1",
                metadata={},
            )

    stub_instance = _StubParser()

    def _provider() -> object:
        """Return the pre-built stub instance so FastAPI skips constructor introspection."""
        return stub_instance

    app.dependency_overrides[get_document_parser] = _provider
    client = TestClient(app)

    response = client.post(
        "/parse/upload",
        files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 200
    assert response.json()["document"]["page_count"] == sentinel_page_count
    assert response.json()["document"]["extra"]["parser_version"] == "stub-v1"


def test_legacy_parse_endpoint_keeps_m1_contract() -> None:
    """The object-key mock endpoint still returns the M1 shape."""
    app = create_app(Settings(mq_consumer_enabled=False))
    client = TestClient(app)

    response = client.post("/parse", json={"pdfObjectKey": "uploads/demo.pdf"})

    assert response.status_code == 200
    assert response.json()["document"]["source"] == "uploads/demo.pdf"
    assert response.json()["document"]["page_count"] == 1
