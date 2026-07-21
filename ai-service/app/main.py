"""FinReport AI service FastAPI application."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.models import router as models_router
from app.api.parse import router as parse_router
from app.core.config import Settings
from app.core.exceptions import AiException
from app.mq.consumer import TaskConsumer
from app.mq.producer import ProgressProducer


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create a configured FastAPI application instance.

    Args:
        settings: Optional settings override for tests.

    Returns:
        Configured ASGI application.
    """
    runtime_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Start and stop the M1 RabbitMQ worker with the web application."""
        producer = ProgressProducer(runtime_settings)
        consumer = TaskConsumer(runtime_settings, producer)
        app.state.task_consumer = consumer
        consumer.start()
        try:
            yield
        finally:
            consumer.stop()

    application = FastAPI(
        title="FinReport AI Service",
        description="A 股上市公司财报深度解析 Agent — L3 AI 服务层",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.exception_handler(AiException)
    async def handle_ai_exception(_: Request, exc: AiException) -> JSONResponse:
        """Map L3 AiException to a stable 500 JSON envelope (RFC 9457-ish).

        Args:
            _: The incoming request (unused).
            exc: The raised AiException.

        Returns:
            A JSON response with a human-readable detail and 500 status.
        """
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    application.include_router(health_router)
    application.include_router(parse_router)
    application.include_router(models_router)
    return application


app = create_app()
