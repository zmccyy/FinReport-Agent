"""M2 parse MQ handler: MinIO fetch + DocumentParser."""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.exceptions import AiException
from app.core.minio_client import MinioObjectClient, ObjectStore
from app.modules.parser.document_parser import DocumentParser
from app.modules.parser.parser_factory import create_document_parser
from app.schemas.task import TaskMessage
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

_parser: DocumentParser | None = None
_object_store: ObjectStore | None = None


def configure_handler(
    *,
    parser: DocumentParser | None = None,
    object_store: ObjectStore | None = None,
) -> None:
    """Inject parser/object-store dependencies (used by unit tests).

    Args:
        parser: Optional DocumentParser override.
        object_store: Optional MinIO/object-store override.
    """
    global _parser, _object_store
    _parser = parser
    _object_store = object_store


def reset_handler() -> None:
    """Clear injected dependencies so defaults are rebuilt lazily."""
    configure_handler(parser=None, object_store=None)


def _resolve_parser() -> DocumentParser:
    """Return the configured parser or build the production default."""
    if _parser is not None:
        return _parser
    return create_document_parser(
        Settings(),
        enable_table_recognition=False,
        enable_ocr=False,
    )


def _resolve_object_store() -> ObjectStore:
    """Return the configured object store or build the production default."""
    if _object_store is not None:
        return _object_store
    return MinioObjectClient(Settings())


def _serialize_document(document: Any) -> dict[str, Any]:
    """Convert a parsed Document into the MQ progress result envelope.

    The nested ``document`` object keeps the M1 camelCase contract
    (``pageCount``) while ``extra`` carries the full M6 schema payload.
    """
    payload = document.model_dump(mode="json")
    return {
        "document": {
            "source": document.source,
            "pageCount": document.page_count,
            "tableCount": document.total_tables,
            "extra": payload,
        }
    }


async def handle(message: TaskMessage) -> dict[str, Any]:
    """Fetch a PDF from MinIO and parse it into a Document result.

    Args:
        message: Validated parse task message containing ``pdfObjectKey``.

    Returns:
        Parsed-document metadata plus the full Document payload under ``extra``.

    Raises:
        AiException: When the payload is invalid or MinIO fetch fails.
    """
    pdf_object_key = message.payload.get("pdfObjectKey")
    if not pdf_object_key:
        raise AiException("Missing pdfObjectKey in parse task payload")

    object_key = str(pdf_object_key)
    LOGGER.info(
        "[handle] taskId=%s pdfObjectKey=%s",
        message.task_id,
        object_key,
    )

    pdf_bytes = _resolve_object_store().fetch_bytes(object_key)
    document = _resolve_parser().parse_bytes(pdf_bytes, source=object_key)
    return _serialize_document(document)
