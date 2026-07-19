"""M1 mock parse endpoint."""

from fastapi import APIRouter

from app.schemas.parse import MockDocument, ParseRequest, ParseResponse

router = APIRouter(tags=["parser"])


@router.post("/parse", response_model=ParseResponse)
async def parse_document(request: ParseRequest) -> ParseResponse:
    """Return a deterministic placeholder document for M1 integration tests.

    Args:
        request: PDF object-key request.

    Returns:
        A mock document that identifies the requested object.
    """
    return ParseResponse(document=MockDocument(source=request.pdf_object_key))
