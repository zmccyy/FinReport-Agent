"""M2.04 ModelHub + LlmLoader tests (M4.08: GPU 栈用例已随 TransformersBackend 移除)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.models import get_modelhub_dep
from app.core.config import Settings
from app.core.exceptions import (
    AiException,
    ModelLoadException,
)
from app.main import create_app
from app.modules.modelhub import (
    QUANT_API,
    ModelHub,
    Scene,
    get_modelhub,
    reset_modelhub,
)
from app.modules.modelhub.llm_loader import (
    GenerateResult,
    LlmLoader,
)

# ---------------------------------------------------------------------------
# Fake backend for LlmLoader / ModelHub tests
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Records calls and returns canned GenerateResult values."""

    def __init__(
        self,
        *,
        result: GenerateResult | None = None,
        load_error: Exception | None = None,
        generate_error: Exception | None = None,
        sleep_seconds: float = 0.0,
    ) -> None:
        self.result = result or GenerateResult(
            text="hello",
            prompt_tokens=2,
            completion_tokens=1,
            latency_ms=10.0,
            first_token_ms=5.0,
        )
        self.load_error = load_error
        self.generate_error = generate_error
        self.sleep_seconds = sleep_seconds
        self.load_calls: list[tuple[str, str]] = []
        self.unload_calls: int = 0
        self.generate_calls: list[dict[str, Any]] = []
        self._loaded_key: str | None = None

    def load(self, model_key: str, quant: str) -> None:
        """Record the load call; raise load_error if set."""
        self.load_calls.append((model_key, quant))
        if self.load_error is not None:
            raise self.load_error
        self._loaded_key = model_key

    def generate(
        self,
        prompt: str,
        *,
        max_new_tokens: int,
        temperature: float,
        timeout_seconds: float,
        system_prompt: str | None = None,
        json_mode: bool = False,
    ) -> GenerateResult:
        """Record the call; raise generate_error or sleep, then return result."""
        import time

        self.generate_calls.append(
            {
                "prompt": prompt,
                "max_new_tokens": max_new_tokens,
                "temperature": temperature,
                "timeout_seconds": timeout_seconds,
                "system_prompt": system_prompt,
                "json_mode": json_mode,
            }
        )
        if self.generate_error is not None:
            raise self.generate_error
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        return self.result

    def unload(self) -> None:
        """Record unload; clear loaded key."""
        self.unload_calls += 1
        self._loaded_key = None

    def is_loaded(self) -> bool:
        """Return whether a model is resident."""
        return self._loaded_key is not None

    def loaded_model(self) -> str | None:
        """Return the loaded model path."""
        return self._loaded_key


# ---------------------------------------------------------------------------
# LlmLoader tests
# ---------------------------------------------------------------------------


def test_load_loads_backend_with_key() -> None:
    """load() forwards model_key + quant to the backend (API route)."""
    backend = _FakeBackend()
    loader = LlmLoader(backend=backend)

    loader.load("deepseek-chat", "", QUANT_API)

    assert backend.load_calls == [("deepseek-chat", QUANT_API)]
    assert loader.is_loaded()
    assert loader.loaded_model() == "deepseek-chat"


def test_load_without_backend_fails_loud() -> None:
    """M4.08: no default backend — missing injection raises."""
    loader = LlmLoader()

    with pytest.raises(ModelLoadException, match="explicit backend"):
        loader.load("deepseek-chat", "", QUANT_API)


def test_load_skips_when_same_key_already_loaded() -> None:
    """Repeated load of the same key is a no-op."""
    backend = _FakeBackend()
    loader = LlmLoader(backend=backend)
    loader.load("deepseek-chat", "", QUANT_API)

    loader.load("deepseek-chat", "", QUANT_API)

    assert len(backend.load_calls) == 1
    assert backend.unload_calls == 0


def test_load_unloads_previous_before_loading_new() -> None:
    """Loading a different key first unloads the previous model."""
    backend = _FakeBackend()
    loader = LlmLoader(backend=backend)
    loader.load("deepseek-chat", "", QUANT_API)

    loader.load("deepseek-reasoner", "", QUANT_API)

    assert backend.unload_calls == 1
    assert backend.load_calls == [
        ("deepseek-chat", QUANT_API),
        ("deepseek-reasoner", QUANT_API),
    ]
    assert loader.loaded_model() == "deepseek-reasoner"


def test_load_propagates_model_load_exception() -> None:
    """Backend load errors propagate as ModelLoadException."""
    backend = _FakeBackend(
        load_error=ModelLoadException("missing weights"),
    )
    loader = LlmLoader(backend=backend)

    with pytest.raises(ModelLoadException, match="missing weights"):
        loader.load("deepseek-chat", "", QUANT_API)

    assert not loader.is_loaded()


def test_generate_raises_when_not_loaded() -> None:
    """generate() without a loaded model raises AiException."""
    loader = LlmLoader(backend=_FakeBackend())

    with pytest.raises(AiException, match="No LLM loaded"):
        loader.generate("hello")


def test_generate_delegates_to_backend_with_defaults() -> None:
    """generate() forwards kwargs and applies Settings defaults."""
    backend = _FakeBackend()
    settings = Settings(
        model_max_new_tokens=128,
        model_generate_timeout_seconds=42,
    )
    loader = LlmLoader(settings=settings, backend=backend)
    loader.load("deepseek-chat", "", QUANT_API)

    result = loader.generate("hello", temperature=0.3)

    assert result.text == "hello"
    call = backend.generate_calls[0]
    assert call["prompt"] == "hello"
    assert call["max_new_tokens"] == 128
    assert call["temperature"] == 0.3
    assert call["timeout_seconds"] == 42.0


def test_generate_overrides_defaults() -> None:
    """Explicit kwargs override Settings defaults."""
    backend = _FakeBackend()
    loader = LlmLoader(backend=backend)
    loader.load("deepseek-chat", "", QUANT_API)

    loader.generate(
        "hi",
        max_new_tokens=8,
        temperature=0.5,
        timeout_seconds=10.0,
    )

    call = backend.generate_calls[0]
    assert call["max_new_tokens"] == 8
    assert call["temperature"] == 0.5
    assert call["timeout_seconds"] == 10.0


def test_unload_clears_state() -> None:
    """unload() forwards to backend and clears the loaded key."""
    backend = _FakeBackend()
    loader = LlmLoader(backend=backend)
    loader.load("deepseek-chat", "", QUANT_API)

    loader.unload()

    assert backend.unload_calls == 1
    assert not loader.is_loaded()
    assert loader.loaded_model() is None


def test_unload_when_not_loaded_is_noop() -> None:
    """unload() with nothing loaded does nothing."""
    backend = _FakeBackend()
    loader = LlmLoader(backend=backend)

    loader.unload()

    assert backend.unload_calls == 0


# ---------------------------------------------------------------------------
# ModelHub tests
# ---------------------------------------------------------------------------


def test_modelhub_load_llm_delegates_to_loader() -> None:
    """load_llm() forwards the logical key + api quant to LlmLoader."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))

    hub.load_llm("deepseek-chat", QUANT_API)

    assert backend.load_calls == [("deepseek-chat", QUANT_API)]
    assert hub.llm_loader.loaded_model() == "deepseek-chat"


def test_modelhub_load_llm_rejects_non_api_quant() -> None:
    """M4.08: local quant labels surface as ModelLoadException."""
    hub = ModelHub(llm_loader=LlmLoader(backend=_FakeBackend()))

    with pytest.raises(ModelLoadException, match="GPU stack was removed"):
        hub.load_llm("deepseek-chat", "gptq-int4")


def test_modelhub_generate_delegates_to_loader() -> None:
    """generate() forwards to LlmLoader.generate()."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    hub.load_llm("deepseek-chat", QUANT_API)

    result = hub.generate("prompt", max_new_tokens=16)

    assert result.text == "hello"
    assert backend.generate_calls[0]["prompt"] == "prompt"


def test_modelhub_embed_not_yet_implemented() -> None:
    """embed() raises AiException until M4.07 wires bge-small-zh-v1.5."""
    hub = ModelHub(llm_loader=LlmLoader(backend=_FakeBackend()))

    with pytest.raises(AiException, match="M4.07"):
        hub.embed(["hello"])


def test_modelhub_route_returns_scene_model_pair() -> None:
    """route() returns the (model_key, quant) tuple for each scene."""
    hub = ModelHub(llm_loader=LlmLoader(backend=_FakeBackend()))

    assert hub.route(Scene.REASON) == ("deepseek-chat", QUANT_API)
    assert hub.route(Scene.EXTRACT) == ("deepseek-chat", QUANT_API)
    assert hub.route(Scene.EMBED) == ("bge", "cpu")


def test_modelhub_load_for_scene_loads_llm() -> None:
    """load_for_scene() loads the API model for the REASON scene."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))

    hub.load_for_scene(Scene.REASON)

    assert hub.llm_loader.loaded_model() == "deepseek-chat"
    assert backend.load_calls[0][1] == QUANT_API


def test_modelhub_load_for_scene_rejects_non_llm_scene() -> None:
    """load_for_scene() raises for EMBED (wired in M4.07)."""
    hub = ModelHub(llm_loader=LlmLoader(backend=_FakeBackend()))

    with pytest.raises(AiException, match="does not route"):
        hub.load_for_scene(Scene.EMBED)


def test_modelhub_unload_forwards_to_loader() -> None:
    """unload() delegates to LlmLoader.unload()."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    hub.load_llm("deepseek-chat", QUANT_API)

    hub.unload()

    assert backend.unload_calls == 1
    assert not hub.is_loaded_status()


def test_modelhub_status_returns_snapshot() -> None:
    """status() reports loaded_llm + scene routing."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))

    status = hub.status()
    assert status["loaded_llm"] is None
    assert status["is_loaded"] is False
    assert status["scenes"]["reason"] == {"model": "deepseek-chat", "quant": QUANT_API}

    hub.load_llm("deepseek-chat", QUANT_API)
    status = hub.status()
    assert status["loaded_llm"] == "deepseek-chat"
    assert status["is_loaded"] is True


def test_modelhub_status_unknown_model_key_isolated() -> None:
    """status() reflects the loader state after a failed load."""
    backend = _FakeBackend(load_error=ModelLoadException("boom"))
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))

    with pytest.raises(ModelLoadException):
        hub.load_llm("deepseek-chat", QUANT_API)

    status = hub.status()
    assert status["loaded_llm"] is None
    assert status["is_loaded"] is False


def test_get_modelhub_returns_singleton() -> None:
    """get_modelhub() returns the same instance across calls."""
    reset_modelhub(None)
    try:
        hub_a = get_modelhub()
        hub_b = get_modelhub()
        assert hub_a is hub_b
    finally:
        reset_modelhub(None)


def test_reset_modelhub_clears_singleton() -> None:
    """reset_modelhub(None) clears the cached singleton."""
    reset_modelhub(None)
    hub_a = get_modelhub()
    reset_modelhub(None)
    hub_b = get_modelhub()
    try:
        assert hub_a is not hub_b
    finally:
        reset_modelhub(None)


# Add a small helper to ModelHub for test readability.
def _hub_is_loaded_status(self: ModelHub) -> bool:
    """Return the loader's loaded state."""
    return self.llm_loader.is_loaded()


ModelHub.is_loaded_status = _hub_is_loaded_status  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


def _client_with_hub(hub: ModelHub) -> TestClient:
    """Build a TestClient with the ModelHub dependency overridden."""
    app = create_app(Settings(mq_consumer_enabled=False))
    app.dependency_overrides[get_modelhub_dep] = lambda: hub
    return TestClient(app)


def test_internal_models_load_endpoint() -> None:
    """POST /internal/models/load forwards to ModelHub.load_llm."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    client = _client_with_hub(hub)

    response = client.post(
        "/internal/models/load",
        json={"model": "deepseek-chat", "quant": QUANT_API},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"model": "deepseek-chat", "quant": QUANT_API, "loaded": True}
    assert backend.load_calls[0][1] == QUANT_API


def test_internal_models_status_endpoint_reports_loaded() -> None:
    """GET /internal/models/status reflects loaded state + scene map."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    hub.load_llm("deepseek-chat", QUANT_API)
    client = _client_with_hub(hub)

    response = client.get("/internal/models/status")

    assert response.status_code == 200
    body = response.json()
    assert body["loaded_llm"] == "deepseek-chat"
    assert body["is_loaded"] is True
    assert body["scenes"]["reason"]["quant"] == QUANT_API
    assert body["scenes"]["extract"]["model"] == "deepseek-chat"


def test_internal_models_generate_endpoint() -> None:
    """POST /internal/models/generate returns text + timings."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    hub.load_llm("deepseek-chat", QUANT_API)
    client = _client_with_hub(hub)

    response = client.post(
        "/internal/models/generate",
        json={"prompt": "hello", "max_new_tokens": 16, "temperature": 0.0},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "hello"
    assert body["prompt_tokens"] == 2
    assert body["completion_tokens"] == 1
    assert body["latency_ms"] >= 0


def test_internal_models_generate_endpoint_maps_ai_exception_to_500() -> None:
    """AiException from generate() surfaces as 500 JSON envelope."""
    backend = _FakeBackend(generate_error=AiException("inference failed"))
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    hub.load_llm("deepseek-chat", QUANT_API)
    client = _client_with_hub(hub)

    response = client.post(
        "/internal/models/generate",
        json={"prompt": "hello"},
    )

    assert response.status_code == 500
    assert "inference failed" in response.json()["detail"]


def test_internal_models_unload_endpoint_returns_status() -> None:
    """POST /internal/models/unload unloads the resident LLM."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    hub.load_llm("deepseek-chat", QUANT_API)
    client = _client_with_hub(hub)

    response = client.post("/internal/models/unload")

    assert response.status_code == 200
    body = response.json()
    assert body["unloaded"] is True
    assert body["status"]["loaded_llm"] is None
    assert backend.unload_calls == 1


def test_internal_models_load_maps_model_load_exception_to_500() -> None:
    """ModelLoadException from load_llm() surfaces as 500 JSON envelope."""
    backend = _FakeBackend(load_error=ModelLoadException("missing weights"))
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    client = _client_with_hub(hub)

    response = client.post(
        "/internal/models/load",
        json={"model": "deepseek-chat", "quant": QUANT_API},
    )

    assert response.status_code == 500
    assert "missing weights" in response.json()["detail"]
