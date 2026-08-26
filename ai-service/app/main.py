"""FinReport AI service FastAPI application."""

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.api.models import router as models_router
from app.api.parse import router as parse_router
from app.core import metrics
from app.core.config import Settings
from app.core.exceptions import AiException
from app.core.tracing import setup_tracing
from app.mq.chat_consumer import ChatConsumer
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
        """Start and stop the M1 RabbitMQ workers with the web application."""
        producer = ProgressProducer(runtime_settings)
        consumer = TaskConsumer(runtime_settings, producer)
        chat_consumer = ChatConsumer(runtime_settings)
        app.state.task_consumer = consumer
        consumer.start()
        chat_consumer.start()
        try:
            yield
        finally:
            consumer.stop()
            chat_consumer.stop()

    application = FastAPI(
        title="FinReport AI Service",
        description="A 股上市公司财报深度解析 Agent — L3 AI 服务层",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def metrics_middleware(request: Request, call_next):
        """Record per-request counters/histograms (M6.05).

        SSE 长连接（/internal/chat/stream）计时到响应头返回为止——流式
        token 的全程时长由 L2 侧问答指标覆盖。
        """
        started = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - started
        route = request.scope.get("route")
        # 优先用路由模板（/reports/{id}）避免标签基数爆炸；无路由（404）用路径。
        path = getattr(route, "path", request.url.path)
        metrics.HTTP_REQUESTS_TOTAL.labels(
            method=request.method, path=path, status=str(response.status_code)
        ).inc()
        metrics.HTTP_REQUEST_DURATION.labels(method=request.method, path=path).observe(
            duration
        )
        return response

    @application.get("/metrics")
    async def prometheus_metrics() -> Response:
        """Expose Prometheus exposition payload (M6.05)."""
        return Response(
            content=metrics.render_metrics(),
            media_type=metrics.metrics_content_type(),
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
    application.include_router(chat_router)
    # M6.07：OTel 链路追踪（环境变量 OTEL_TRACES_ENABLED 驱动，默认关闭）。
    setup_tracing(application)
    return application


app = create_app()
