"""M2/M4.03 parse MQ handler: MinIO fetch + DocumentParser + 产物落 MinIO."""

from __future__ import annotations

import json
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

# M4.03 parse 中间产物对象 key 前缀（finreport-artifacts 桶）。
PARSED_KEY_PREFIX = "parsed/"


def parsed_object_key(task_id: str) -> str:
    """Build the MinIO key holding a task's parse payload.

    Args:
        task_id: Task identifier.

    Returns:
        Key of the form ``parsed/{taskId}.json`` (decision record #6).
    """
    return f"{PARSED_KEY_PREFIX}{task_id}.json"


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
    settings = Settings()
    return create_document_parser(
        settings,
        enable_table_recognition=settings.parser_enable_table_recognition,
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
    """Fetch a PDF from MinIO, parse it, and persist the payload to MinIO.

    M4.03: parse 产物（完整 Document schema JSON）写入
    ``s3://finreport-artifacts/parsed/{taskId}.json``，供 M4.04 extract
    handler 拉取（L2 payload 不携带上游 result，走对象存储中转）。

    Args:
        message: Validated parse task message containing ``pdfObjectKey``.

    Returns:
        Parsed-document metadata plus the full Document payload under ``extra``.

    Raises:
        AiException: When the payload is invalid, MinIO fetch/put fails.
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

    store = _resolve_object_store()
    pdf_bytes = store.fetch_bytes(object_key)
    document = _resolve_parser().parse_bytes(pdf_bytes, source=object_key)

    # M4.03 产物落 MinIO：失败即抛错（extract 无法进行，任务应重试）。
    artifact_key = parsed_object_key(message.task_id)
    store.put_bytes(
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False).encode(
            "utf-8"
        ),
        artifact_key,
        content_type="application/json",
    )
    LOGGER.info(
        "[handle] parse 产物已上传 taskId=%s key=%s pages=%d tables=%d",
        message.task_id,
        artifact_key,
        document.page_count,
        document.total_tables,
    )
    return _serialize_document(document)
