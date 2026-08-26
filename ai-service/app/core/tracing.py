"""M6.07 OpenTelemetry tracing for the L3 AI service（可选启用）.

启用条件（环境变量，compose 注入）：
- ``OTEL_TRACES_ENABLED=true``
- ``OTEL_EXPORTER_OTLP_ENDPOINT``（如 ``http://jaeger:4317``）

依赖缺失（本地最小开发环境）或未启用时静默跳过，不影响进程启动——
与 metrics.py 的 no-op 降级哲学一致。

覆盖：FastAPI 入站 span + httpx 出站 traceparent 注入（DeepSeek API 调用
与 L2→L3 链路衔接）。既有业务级 ``headers.traceId`` 透传保持不变：日志
关联用 traceId，链路可视化用 W3C traceId（同一 UUID 经 TraceIdWebFilter
写入 ``traceparent`` 时由 javaagent 提取，两套在 Jaeger UI 可对齐）。
"""

from __future__ import annotations

import os

from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: L3 服务名（Jaeger service name）。
SERVICE_NAME = "finreport-ai-service"


def tracing_enabled() -> bool:
    """Return True when tracing should initialize (env-driven, default off)."""
    return os.getenv("OTEL_TRACES_ENABLED", "").strip().lower() == "true"


def setup_tracing(app: object) -> bool:
    """Initialize OTel tracing and instrument the FastAPI app.

    Args:
        app: The FastAPI application instance.

    Returns:
        True when tracing was initialized; False when disabled or the
        opentelemetry dependencies are not installed.
    """
    if not tracing_enabled():
        LOGGER.debug("[tracing] OTEL_TRACES_ENABLED 未开启,跳过初始化")
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as error:
        LOGGER.warning(
            "[tracing] opentelemetry 依赖缺失,跳过链路追踪: %s "
            "(安装 prod extras 可启用)", error
        )
        return False

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": os.getenv("OTEL_SERVICE_NAME", SERVICE_NAME),
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)

    # httpx 全局注入：DeepSeekBackend 的 httpx.Client 自动携带 traceparent。
    HTTPXClientInstrumentor().instrument()
    FastAPIInstrumentor.instrument_app(app)  # type: ignore[arg-type]
    LOGGER.info("[tracing] OTel 已初始化 endpoint=%s service=%s", endpoint, SERVICE_NAME)
    return True
