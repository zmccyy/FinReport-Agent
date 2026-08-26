"""M6.05 Prometheus metrics for the L3 AI service.

指标设计（spec §7.2.3 核心指标）：
- HTTP：``fin_http_requests_total`` / ``fin_http_request_duration_seconds``
- 阶段：``fin_stage_total`` / ``fin_stage_duration_seconds``（PARSE/EXTRACT_*/CHECK/REPORT）
- LLM：``fin_llm_calls_total`` / ``fin_llm_call_duration_seconds`` /
  ``fin_llm_tokens_total`` / ``fin_llm_retries_total``（DeepSeek API）
- Embedding：``fin_embed_duration_seconds`` / ``fin_embed_total``

容错：prometheus_client 未安装时（本地最小开发环境）所有 record 函数
退化为 no-op，不影响业务进程启动——与 embedder 的惰性加载哲学一致。
"""

from __future__ import annotations

import time
from typing import Any

try:  # pragma: no cover - import branch depends on env
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _NoopMetric:  # type: ignore[no-redef]
        """Duck-typed no-op so call sites stay unchanged without the lib."""

        def labels(self, *args: Any, **kwargs: Any) -> "_NoopMetric":
            return self

        def inc(self, *args: Any, **kwargs: Any) -> None:
            return None

        def observe(self, *args: Any, **kwargs: Any) -> None:
            return None

        def set(self, *args: Any, **kwargs: Any) -> None:
            return None

    Counter = Histogram = Gauge = _NoopMetric  # type: ignore[assignment,misc]

    def generate_latest() -> bytes:  # type: ignore[misc]
        return b""


if _PROMETHEUS_AVAILABLE:
    # 命名约定：Counter 名不带 _total 后缀——prometheus_client 会自动追加，
    # 显式写 _total 会得到 `fin_xxx_total_total`。
    HTTP_REQUESTS_TOTAL = Counter(
        "fin_http_requests", "HTTP 请求总数", ["method", "path", "status"]
    )
    HTTP_REQUEST_DURATION = Histogram(
        "fin_http_request_duration_seconds", "HTTP 请求耗时（秒）", ["method", "path"]
    )
    STAGE_TOTAL = Counter(
        "fin_stage", "任务阶段处理总数", ["stage", "outcome"]
    )
    STAGE_DURATION = Histogram(
        "fin_stage_duration_seconds",
        "任务阶段处理耗时（秒）",
        ["stage"],
        buckets=(1, 5, 15, 30, 60, 120, 300, 600),
    )
    LLM_CALLS_TOTAL = Counter(
        "fin_llm_calls", "LLM API 调用总数", ["model", "outcome"]
    )
    LLM_CALL_DURATION = Histogram(
        "fin_llm_call_duration_seconds",
        "LLM API 单次调用耗时（秒）",
        ["model"],
        buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
    )
    LLM_TOKENS_TOTAL = Counter(
        "fin_llm_tokens", "LLM token 用量", ["model", "type"]
    )
    LLM_RETRIES_TOTAL = Counter(
        "fin_llm_retries", "LLM API 重试次数", ["model", "reason"]
    )
    EMBED_TOTAL = Counter(
        "fin_embed", "embedding 调用总数", ["outcome"]
    )
    EMBED_DURATION = Histogram(
        "fin_embed_duration_seconds",
        "embedding 批次耗时（秒）",
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
    )
    CHAT_SESSIONS_ACTIVE = Gauge(
        "fin_chat_sessions_active", "进行中的问答会话数（chat consumer）"
    )
else:  # pragma: no cover
    HTTP_REQUESTS_TOTAL = Counter("noop", "", [])  # type: ignore[call-arg]
    HTTP_REQUEST_DURATION = Histogram("noop", "", [])  # type: ignore[call-arg]
    STAGE_TOTAL = Counter("noop", "", [])  # type: ignore[call-arg]
    STAGE_DURATION = Histogram("noop", "", [])  # type: ignore[call-arg]
    LLM_CALLS_TOTAL = Counter("noop", "", [])  # type: ignore[call-arg]
    LLM_CALL_DURATION = Histogram("noop", "", [])  # type: ignore[call-arg]
    LLM_TOKENS_TOTAL = Counter("noop", "", [])  # type: ignore[call-arg]
    LLM_RETRIES_TOTAL = Counter("noop", "", [])  # type: ignore[call-arg]
    EMBED_TOTAL = Counter("noop", "", [])  # type: ignore[call-arg]
    EMBED_DURATION = Histogram("noop", "", [])  # type: ignore[call-arg]
    CHAT_SESSIONS_ACTIVE = Gauge("noop", "", [])  # type: ignore[call-arg]


def render_metrics() -> bytes:
    """Render the Prometheus exposition format payload.

    Returns:
        Bytes suitable as the ``/metrics`` response body.
    """
    return generate_latest()


def metrics_content_type() -> str:
    """Return the Content-Type for the exposition payload."""
    return CONTENT_TYPE_LATEST


class StageTimer:
    """Context manager recording one task-stage execution (duration+outcome).

    Usage::

        with StageTimer("PARSE") as timer:
            ...
        # timer.outcome 由异常路径自动置为 "failed"
    """

    def __init__(self, stage: str) -> None:
        self._stage = stage
        self._start = 0.0
        self.outcome = "success"

    def __enter__(self) -> "StageTimer":
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        duration = time.monotonic() - self._start
        if exc_type is not None:
            self.outcome = "failed"
        STAGE_DURATION.labels(stage=self._stage).observe(duration)
        STAGE_TOTAL.labels(stage=self._stage, outcome=self.outcome).inc()
