"""FinReport AI service FastAPI application."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.parse import router as parse_router
from app.core.config import Settings
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
    application.include_router(health_router)
    application.include_router(parse_router)
    return application


app = create_app()
