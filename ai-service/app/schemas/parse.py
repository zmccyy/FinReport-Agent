"""HTTP schemas for M1 mock and M2 real document parsing."""

from typing import Any

from pydantic import BaseModel, Field


class ParseRequest(BaseModel):
    """Request identifying a PDF object in MinIO."""

    pdf_object_key: str = Field(alias="pdfObjectKey", min_length=1)


class MockDocument(BaseModel):
    """Parsed-document representation.

    The ``text`` and ``page_count`` fields keep the M1 mock contract intact;
    M2 adds the optional ``extra`` payload carrying the full Document model.
    """

    source: str
    page_count: int = 1
    text: str = "M1 mock document"
    extra: dict[str, Any] | None = None


class ParseResponse(BaseModel):
    """Successful mock or real parse response."""

    document: MockDocument
