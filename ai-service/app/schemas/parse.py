"""HTTP schemas for M1 mock document parsing."""

from pydantic import BaseModel, Field


class ParseRequest(BaseModel):
    """Request identifying a PDF object in MinIO."""

    pdf_object_key: str = Field(alias="pdfObjectKey", min_length=1)


class MockDocument(BaseModel):
    """Minimal parsed-document representation used before M2 parsing."""

    source: str
    page_count: int = 1
    text: str = "M1 mock document"


class ParseResponse(BaseModel):
    """Successful mock parse response."""

    document: MockDocument
