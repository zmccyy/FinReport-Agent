"""M1 mock financial-statement extraction handler."""

from typing import Any

from app.schemas.task import TaskMessage


async def handle(message: TaskMessage) -> dict[str, Any]:
    """Produce deterministic mock extraction output.

    Args:
        message: Validated extract task message.

    Returns:
        Mock statement marker for later M2 replacement.
    """
    return {"statement": message.step, "items": []}
