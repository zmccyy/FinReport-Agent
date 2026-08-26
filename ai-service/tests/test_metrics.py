"""M6.05 metrics 模块测试：指标注册 / StageTimer / /metrics 端点。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core import metrics
from app.main import create_app


def _fresh_app() -> TestClient:
    """Create a test client with MQ consumers disabled (unit scope)."""
    from app.core.config import Settings

    settings = Settings(mq_consumer_enabled=False)
    return TestClient(create_app(settings))


def test_render_metrics_contains_registered_series() -> None:
    result = metrics.StageTimer("PARSE")
    with result:
        pass
    payload = metrics.render_metrics().decode("utf-8")
    assert "fin_stage_total" in payload
    assert "fin_stage_duration_seconds" in payload


def test_stage_timer_records_success_outcome() -> None:
    with metrics.StageTimer("EXTRACT_BS"):
        pass
    payload = metrics.render_metrics().decode("utf-8")
    assert 'stage="EXTRACT_BS"' in payload
    assert 'outcome="success"' in payload


def test_stage_timer_records_failed_outcome_on_exception() -> None:
    try:
        with metrics.StageTimer("CHECK"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    payload = metrics.render_metrics().decode("utf-8")
    assert 'stage="CHECK"' in payload
    assert 'outcome="failed"' in payload


def test_metrics_endpoint_exposes_prometheus_payload() -> None:
    client = _fresh_app()
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "fin_http_requests_total" in response.text


def test_http_middleware_records_request_counters() -> None:
    client = _fresh_app()
    client.get("/internal/health")
    payload = client.get("/metrics").text
    assert 'path="/internal/health"' in payload
    assert 'status="200"' in payload


def test_noop_fallback_shapes_are_duck_compatible() -> None:
    """When prometheus_client is missing the module degrades to no-ops."""
    # 在已安装 prometheus_client 的环境下仅验证接口形状存在。
    assert hasattr(metrics.LLM_CALLS_TOTAL, "labels")
    assert hasattr(metrics.EMBED_DURATION, "observe")
    assert hasattr(metrics.CHAT_SESSIONS_ACTIVE, "set")
