"""M4.02 DeepSeekBackend + ModelHub API 路由单测（mock httpx.MockTransport）。

覆盖（spec §8.1 API 成本与限流约束）：
- 成功调用：payload 组装 / usage 解析 / Authorization 头
- system_prompt 与 json_mode 透传
- 429/5xx 指数退避重试 2 次；4xx 不重试；超时不重试
- 重试耗尽 → AiException；API Key 缺失 → ModelLoadException
- ModelHub 集成：generate() 自动初始化 API 后端（无需显式 load）
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import (
    AiException,
    InferenceTimeoutException,
    ModelLoadException,
)
from app.modules.modelhub.api_backend import QUANT_API, DeepSeekBackend
from app.modules.modelhub.modelhub import ModelHub, Scene


def _settings(**overrides: Any) -> Settings:
    """Test settings: fake key + zero retry delay (no real sleeping)."""
    defaults: dict[str, Any] = {
        "llm_api_key": "test-key",
        "llm_api_retry_base_delay_seconds": 0.0,
        "llm_api_max_retries": 2,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _ok_body(
    content: str = "你好", prompt_tokens: int = 10, completion_tokens: int = 5
) -> dict[str, Any]:
    """Build a minimal OpenAI-compatible chat completion body."""
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


class _TransportRecorder:
    """MockTransport handler that replays queued responses and records requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _backend(
    recorder: _TransportRecorder, settings: Settings | None = None
) -> DeepSeekBackend:
    return DeepSeekBackend(
        settings=settings or _settings(),
        transport=httpx.MockTransport(recorder.handler),
    )


def test_generate_success_builds_payload_and_parses_usage() -> None:
    """A 2xx response yields a GenerateResult with the API usage stats."""
    recorder = _TransportRecorder(httpx.Response(200, json=_ok_body("答案")))
    backend = _backend(recorder)

    result = backend.generate(
        "抽取三表",
        max_new_tokens=256,
        temperature=0.1,
        timeout_seconds=30.0,
    )

    assert result.text == "答案"
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 5
    assert result.latency_ms >= 0.0

    request = recorder.requests[0]
    assert request.url.path == "/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-key"
    payload = json.loads(request.content)
    assert payload["model"] == "deepseek-chat"
    assert payload["messages"] == [{"role": "user", "content": "抽取三表"}]
    assert payload["max_tokens"] == 256
    assert payload["temperature"] == 0.1
    assert payload["stream"] is False
    assert "response_format" not in payload


def test_generate_system_prompt_and_json_mode() -> None:
    """system_prompt becomes a system message; json_mode sets response_format."""
    recorder = _TransportRecorder(httpx.Response(200, json=_ok_body("{}")))
    backend = _backend(recorder)

    backend.generate(
        "返回 json",
        max_new_tokens=64,
        temperature=0.0,
        timeout_seconds=10.0,
        system_prompt="你是财报抽取助手",
        json_mode=True,
    )

    payload = json.loads(recorder.requests[0].content)
    assert payload["messages"][0] == {"role": "system", "content": "你是财报抽取助手"}
    assert payload["messages"][1] == {"role": "user", "content": "返回 json"}
    assert payload["response_format"] == {"type": "json_object"}


def test_generate_retries_429_then_succeeds() -> None:
    """A 429 is retried and succeeds on the second attempt."""
    recorder = _TransportRecorder(
        httpx.Response(429, text="rate limited"),
        httpx.Response(200, json=_ok_body()),
    )
    backend = _backend(recorder)

    result = backend.generate(
        "p", max_new_tokens=8, temperature=0.0, timeout_seconds=5.0
    )

    assert result.text == "你好"
    assert len(recorder.requests) == 2


def test_generate_retries_5xx_twice_then_succeeds() -> None:
    """500 x2 retried (max_retries=2) then 200 succeeds — 3 total attempts."""
    recorder = _TransportRecorder(
        httpx.Response(500, text="boom"),
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, json=_ok_body()),
    )
    backend = _backend(recorder)

    result = backend.generate(
        "p", max_new_tokens=8, temperature=0.0, timeout_seconds=5.0
    )

    assert result.text == "你好"
    assert len(recorder.requests) == 3


def test_generate_4xx_does_not_retry() -> None:
    """A 400 raises AiException immediately with a single request."""
    recorder = _TransportRecorder(httpx.Response(400, text="bad request"))
    backend = _backend(recorder)

    with pytest.raises(AiException, match="400"):
        backend.generate("p", max_new_tokens=8, temperature=0.0, timeout_seconds=5.0)

    assert len(recorder.requests) == 1


def test_generate_timeout_raises_without_retry() -> None:
    """Timeouts surface as InferenceTimeoutException and are not retried."""
    recorder = _TransportRecorder(httpx.ReadTimeout("timed out"))
    backend = _backend(recorder)

    with pytest.raises(InferenceTimeoutException, match="timed out"):
        backend.generate("p", max_new_tokens=8, temperature=0.0, timeout_seconds=5.0)

    assert len(recorder.requests) == 1


def test_generate_network_error_retries_then_raises() -> None:
    """Transport errors are retried; exhaustion raises AiException."""
    recorder = _TransportRecorder(
        httpx.ConnectError("conn refused"),
        httpx.ConnectError("conn refused"),
        httpx.ConnectError("conn refused"),
    )
    backend = _backend(recorder)

    with pytest.raises(AiException, match="3 attempts"):
        backend.generate("p", max_new_tokens=8, temperature=0.0, timeout_seconds=5.0)

    assert len(recorder.requests) == 3


def test_generate_retries_exhausted_on_5xx() -> None:
    """Persistent 503 exhausts retries and raises AiException."""
    recorder = _TransportRecorder(
        httpx.Response(503, text="down"),
        httpx.Response(503, text="down"),
        httpx.Response(503, text="down"),
    )
    backend = _backend(recorder)

    with pytest.raises(AiException, match="503"):
        backend.generate("p", max_new_tokens=8, temperature=0.0, timeout_seconds=5.0)

    assert len(recorder.requests) == 3


def test_generate_malformed_body_raises() -> None:
    """A 2xx with a malformed body raises AiException."""
    recorder = _TransportRecorder(httpx.Response(200, text="not json"))
    backend = _backend(recorder)

    with pytest.raises(AiException, match="malformed"):
        backend.generate("p", max_new_tokens=8, temperature=0.0, timeout_seconds=5.0)


def test_generate_reasoning_truncation_raises_clear_error() -> None:
    """Empty content + finish_reason=length must fail with a diagnostic error.

    M4.10 回归：reasoning 模型（deepseek-v4-flash）把 token 预算花在
    reasoning_content 上，content 为空——此前静默返回空串，三表抽取
    全部 "empty model output"（validator 只见空输出，根因难查）。
    """
    body = _ok_body(content="")
    body["choices"][0]["finish_reason"] = "length"
    body["choices"][0]["message"]["reasoning_content"] = "spent on reasoning"
    recorder = _TransportRecorder(httpx.Response(200, json=body))
    backend = _backend(recorder)

    with pytest.raises(AiException, match="truncated"):
        backend.generate("p", max_new_tokens=8, temperature=0.0, timeout_seconds=5.0)


def test_load_without_api_key_raises() -> None:
    """Missing LLM_API_KEY fails fast with ModelLoadException."""
    backend = DeepSeekBackend(settings=_settings(llm_api_key=""))

    with pytest.raises(ModelLoadException, match="LLM_API_KEY"):
        backend.generate("p", max_new_tokens=8, temperature=0.0, timeout_seconds=5.0)


def test_unload_closes_client() -> None:
    """unload() drops the client; loaded_model() becomes None."""
    recorder = _TransportRecorder(httpx.Response(200, json=_ok_body()))
    backend = _backend(recorder)
    backend.load("", QUANT_API)

    assert backend.is_loaded() is True
    assert backend.loaded_model() == "deepseek-chat"

    backend.unload()

    assert backend.is_loaded() is False
    assert backend.loaded_model() is None


def test_modelhub_generate_auto_loads_api_backend() -> None:
    """ModelHub.generate() works without explicit load_llm() (M4.02)."""
    recorder = _TransportRecorder(httpx.Response(200, json=_ok_body("ok")))
    settings = _settings()
    hub = ModelHub(
        settings=settings,
        llm_backend=DeepSeekBackend(
            settings=settings,
            transport=httpx.MockTransport(recorder.handler),
        ),
    )

    result = hub.generate("hello", json_mode=False)

    assert result.text == "ok"
    assert hub.llm_loader.loaded_model() == "deepseek-chat"
    assert len(recorder.requests) == 1


def test_route_when_api_pivot_scenes_route_to_deepseek() -> None:
    """Scene routing reflects the 2026-08-16 API pivot."""
    hub = ModelHub(
        llm_backend=DeepSeekBackend(
            settings=_settings(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=_ok_body())
            ),
        )
    )

    assert hub.route(Scene.EXTRACT) == ("deepseek-chat", QUANT_API)
    assert hub.route(Scene.REASON) == ("deepseek-chat", QUANT_API)
    assert hub.route(Scene.EMBED) == ("bge", "cpu")
