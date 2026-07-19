"""M1 mock parser handler."""

from typing import Any

from app.schemas.task import TaskMessage


async def handle(message: TaskMessage) -> dict[str, Any]:
    """Produce mock parse metadata for the task.

    Args:
        message: Validated parse task message.

    Returns:
        Minimal parsed-document metadata.
    """
    return {
        "document": {"source": message.payload.get("pdfObjectKey", ""), "pageCount": 1}
    }
