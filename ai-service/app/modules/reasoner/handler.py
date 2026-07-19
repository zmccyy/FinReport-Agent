"""M1 mock checking and report-generation handler."""

from typing import Any

from app.schemas.task import TaskMessage


async def handle(message: TaskMessage) -> dict[str, Any]:
    """Produce deterministic mock checking or report output.

    Args:
        message: Validated check/report task message.

    Returns:
        Mock result that lets M1 advance the state machine.
    """
    return {"operation": message.step, "status": "mock-complete"}
