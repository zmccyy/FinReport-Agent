"""Health check endpoint."""

from fastapi import APIRouter

router = APIRouter(tags=["system"])


@router.get("/internal/health")
async def health() -> dict[str, str]:
    """Return the liveness response consumed by Docker Compose.

    Returns:
        Service liveness details.
    """
    return {"status": "UP", "service": "finreport-ai-service"}
