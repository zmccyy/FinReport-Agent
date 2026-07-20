"""M2.01 parse endpoint: object-key mock + real PDF byte upload paths."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, UploadFile

from app.core.exceptions import AiException
from app.modules.parser.document_parser import DocumentParser
from app.schemas.parse import MockDocument, ParseRequest, ParseResponse
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)
router = APIRouter(tags=["parser"])


def get_document_parser() -> Any:
    """Provide the default DocumentParser instance.

    The return type is intentionally unannotated so FastAPI does not introspect
    the ``DocumentParser`` constructor (which carries non-pydantic Protocol
    arguments). Tests can override this dependency via
    ``app.dependency_overrides[get_document_parser] = ...``.
    """
    return DocumentParser()


@router.post("/parse", response_model=ParseResponse)
async def parse_document(request: ParseRequest) -> ParseResponse:
    """Return a placeholder document for M1 object-key integration tests.

    Real MinIO-backed parsing is wired through the MQ consumer; this endpoint
    keeps the M1 contract green for callers that only need the object key.

    Args:
        request: PDF object-key request.

    Returns:
        A mock document that identifies the requested object.
    """
    return ParseResponse(document=MockDocument(source=request.pdf_object_key))


@router.post("/parse/upload", response_model=ParseResponse)
async def parse_upload(
    file: Annotated[UploadFile, File(description="PDF 上传文件")],
    parser: Annotated[Any, Depends(get_document_parser)],
) -> ParseResponse:
    """Parse an uploaded PDF through the real M2 DocumentParser.

    Args:
        file: The PDF multipart upload.
        parser: Injected DocumentParser (override in tests).

    Returns:
        A ParseResponse carrying the parsed Document summary.
    """
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise AiException("Uploaded PDF is empty")
    document = parser.parse_bytes(pdf_bytes, source=file.filename or "upload.pdf")
    payload = document.model_dump(mode="json")
    return ParseResponse(
        document=MockDocument(
            source=document.source,
            page_count=document.page_count,
            text=str(payload.get("parser_version", "")),
            extra=payload,
        )
    )
