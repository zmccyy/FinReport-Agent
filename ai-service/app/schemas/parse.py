"""HTTP schemas for the /parse endpoints (M1 object-key stub + M2 real upload)."""

from typing import Any

from pydantic import BaseModel, Field


class ParseRequest(BaseModel):
    """Request identifying a PDF object in MinIO."""

    pdf_object_key: str = Field(alias="pdfObjectKey", min_length=1)


class DocumentSummary(BaseModel):
    """Parsed-document summary (M2.01, 原 M1 契约).

    ``/parse/upload``（真实解析）填充 ``page_count`` 与 ``extra``（完整
    Document model）；``/parse``（object-key 存根）仅回显 ``source``，
    保留 M1 集成测试契约（``text`` 默认值与字段形状不变）。
    """

    source: str
    page_count: int = 1
    text: str = "M1 mock document"
    extra: dict[str, Any] | None = None


class ParseResponse(BaseModel):
    """Successful parse response (real upload or M1 object-key stub)."""

    document: DocumentSummary
