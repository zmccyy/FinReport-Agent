"""M2.04 ModelHub + LlmLoader tests."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.models import get_modelhub_dep
from app.core.config import Settings
from app.core.exceptions import (
    AiException,
    InferenceTimeoutException,
    ModelLoadException,
)
from app.main import create_app
from app.modules.modelhub import (
    QUANT_GPTQ_INT4,
    QUANT_NF4,
    ModelHub,
    Scene,
    get_modelhub,
    reset_modelhub,
)
from app.modules.modelhub.llm_loader import (
    GenerateResult,
    LlmLoader,
    TransformersBackend,
    _is_oom,
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

    def load(self, model_path: str, quant: str) -> None:
        """Record the load call; raise load_error if set."""
        self.load_calls.append((model_path, quant))
        if self.load_error is not None:
            raise self.load_error
        self._loaded_key = model_path

    def generate(
        self,
        prompt: str,
        *,
        max_new_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> GenerateResult:
        """Record the call; raise generate_error or sleep, then return result."""
        import time

        self.generate_calls.append(
            {
                "prompt": prompt,
                "max_new_tokens": max_new_tokens,
                "temperature": temperature,
                "timeout_seconds": timeout_seconds,
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


def test_load_loads_backend_with_path() -> None:
    """load() forwards model_path + quant to the backend."""
    backend = _FakeBackend()
    loader = LlmLoader(backend=backend)

    loader.load("7b", "models/7b", QUANT_GPTQ_INT4)

    assert backend.load_calls == [("models/7b", QUANT_GPTQ_INT4)]
    assert loader.is_loaded()
    assert loader.loaded_model() == "7b"


def test_load_skips_when_same_key_already_loaded() -> None:
    """Repeated load of the same key is a no-op."""
    backend = _FakeBackend()
    loader = LlmLoader(backend=backend)
    loader.load("7b", "models/7b", QUANT_GPTQ_INT4)

    loader.load("7b", "models/7b", QUANT_GPTQ_INT4)

    assert len(backend.load_calls) == 1
    assert backend.unload_calls == 0


def test_load_unloads_previous_before_loading_new() -> None:
    """Loading a different key first unloads the previous model."""
    backend = _FakeBackend()
    loader = LlmLoader(backend=backend)
    loader.load("7b", "models/7b", QUANT_GPTQ_INT4)

    loader.load("1.5b", "models/1.5b", QUANT_NF4)

    assert backend.unload_calls == 1
    assert backend.load_calls == [
        ("models/7b", QUANT_GPTQ_INT4),
        ("models/1.5b", QUANT_NF4),
    ]
    assert loader.loaded_model() == "1.5b"


def test_load_propagates_model_load_exception() -> None:
    """Backend load errors propagate as ModelLoadException."""
    backend = _FakeBackend(
        load_error=ModelLoadException("missing weights"),
    )
    loader = LlmLoader(backend=backend)

    with pytest.raises(ModelLoadException, match="missing weights"):
        loader.load("7b", "models/7b", QUANT_GPTQ_INT4)

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
    loader.load("7b", "models/7b", QUANT_GPTQ_INT4)

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
    loader.load("7b", "models/7b", QUANT_GPTQ_INT4)

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
    loader.load("7b", "models/7b", QUANT_GPTQ_INT4)

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


def test_modelhub_load_llm_resolves_path_and_delegates() -> None:
    """load_llm() looks up the path by name and forwards to LlmLoader."""
    from pathlib import Path

    backend = _FakeBackend()
    loader = LlmLoader(backend=backend)
    hub = ModelHub(
        settings=Settings(model_7b_path="models/Qwen2.5-7B-Instruct-GPTQ-Int4"),
        llm_loader=loader,
    )

    hub.load_llm("7b", QUANT_GPTQ_INT4)

    # Path.expanduser() returns OS-native separators; compare via str(Path).
    expected_path = str(Path("models/Qwen2.5-7B-Instruct-GPTQ-Int4"))
    assert backend.load_calls == [(expected_path, QUANT_GPTQ_INT4)]
    assert hub.llm_loader.loaded_model() == "7b"


def test_modelhub_load_llm_unknown_name_raises() -> None:
    """Unknown model names surface as ModelLoadException."""
    hub = ModelHub(llm_loader=LlmLoader(backend=_FakeBackend()))

    with pytest.raises(ModelLoadException, match="Unknown model name"):
        hub.load_llm("999b", QUANT_GPTQ_INT4)


def test_modelhub_generate_delegates_to_loader() -> None:
    """generate() forwards to LlmLoader.generate()."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    hub.load_llm("7b", QUANT_GPTQ_INT4)

    result = hub.generate("prompt", max_new_tokens=16)

    assert result.text == "hello"
    assert backend.generate_calls[0]["prompt"] == "prompt"


def test_modelhub_embed_not_yet_implemented() -> None:
    """embed() raises AiException in M2.04 (wired in M5)."""
    hub = ModelHub(llm_loader=LlmLoader(backend=_FakeBackend()))

    with pytest.raises(AiException, match="M5"):
        hub.embed(["hello"])


def test_modelhub_route_returns_scene_model_pair() -> None:
    """route() returns the (model_key, quant) tuple for each scene."""
    hub = ModelHub(llm_loader=LlmLoader(backend=_FakeBackend()))

    assert hub.route(Scene.REASON) == ("7b", QUANT_GPTQ_INT4)
    assert hub.route(Scene.EXTRACT) == ("1.5b", QUANT_NF4)
    assert hub.route(Scene.EMBED) == ("bge", "lora")
    assert hub.route(Scene.LAYOUT) == ("layoutlm", "fp16")


def test_modelhub_load_for_scene_loads_llm() -> None:
    """load_for_scene() loads the 7B for the REASON scene."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))

    hub.load_for_scene(Scene.REASON)

    assert hub.llm_loader.loaded_model() == "7b"
    assert backend.load_calls[0][1] == QUANT_GPTQ_INT4


def test_modelhub_load_for_scene_rejects_non_llm_scene() -> None:
    """load_for_scene() raises for EMBED/LAYOUT in M2.04."""
    hub = ModelHub(llm_loader=LlmLoader(backend=_FakeBackend()))

    with pytest.raises(AiException, match="does not route"):
        hub.load_for_scene(Scene.EMBED)
    with pytest.raises(AiException, match="does not route"):
        hub.load_for_scene(Scene.LAYOUT)


def test_modelhub_unload_forwards_to_loader() -> None:
    """unload() delegates to LlmLoader.unload()."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    hub.load_llm("7b", QUANT_GPTQ_INT4)

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
    assert status["scenes"]["reason"] == {"model": "7b", "quant": QUANT_GPTQ_INT4}

    hub.load_llm("7b", QUANT_GPTQ_INT4)
    status = hub.status()
    assert status["loaded_llm"] == "7b"
    assert status["is_loaded"] is True


def test_modelhub_status_unknown_model_key_isolated() -> None:
    """status() reflects the loader state after a failed load."""
    backend = _FakeBackend(load_error=ModelLoadException("boom"))
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))

    with pytest.raises(ModelLoadException):
        hub.load_llm("7b", QUANT_GPTQ_INT4)

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
# TransformersBackend unit tests
# ---------------------------------------------------------------------------


def test_build_quant_config_gptq_returns_none() -> None:
    """GPTQ-Int4 relies on transformers auto-detection (None config)."""
    config = TransformersBackend._build_quant_config(
        QUANT_GPTQ_INT4, _StubTransformers()
    )
    assert config is None


def test_build_quant_config_nf4_returns_bitsandbytes_config() -> None:
    """NF4 builds a BitsAndBytesConfig via the transformers module."""
    import types

    transformers_stub = _StubTransformers()
    torch_stub = types.ModuleType("torch")
    torch_stub.bfloat16 = "bfloat16"
    config = TransformersBackend._build_quant_config(
        QUANT_NF4, transformers_stub, torch_module=torch_stub
    )
    assert isinstance(config, _StubBitsAndBytesConfig)
    assert config.kwargs["load_in_4bit"] is True
    assert config.kwargs["bnb_4bit_quant_type"] == "nf4"
    assert config.kwargs["bnb_4bit_compute_dtype"] == "bfloat16"
    assert config.kwargs["bnb_4bit_use_double_quant"] is True


def test_build_quant_config_nf4_raises_when_torch_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NF4 without a torch module surfaces ModelLoadException."""
    _block_import(monkeypatch, "torch")
    with pytest.raises(ModelLoadException, match="torch is required for nf4"):
        TransformersBackend._build_quant_config(QUANT_NF4, _StubTransformers())


def test_build_quant_config_unknown_raises() -> None:
    """Unknown quant labels raise ModelLoadException."""
    with pytest.raises(ModelLoadException, match="Unsupported quantization"):
        TransformersBackend._build_quant_config("int8", _StubTransformers())


def test_transformers_backend_load_raises_on_missing_torch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing torch import surfaces as ModelLoadException on load()."""
    _block_import(monkeypatch, "torch")
    backend = TransformersBackend()

    with pytest.raises(ModelLoadException, match="torch is required"):
        backend.load("models/7b", QUANT_GPTQ_INT4)


def test_transformers_backend_load_raises_on_missing_transformers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing transformers import surfaces as ModelLoadException on load()."""
    # Stub torch so _lazy_import_torch() succeeds; we are exercising the
    # transformers-missing error path, not the torch-missing one.
    _install_torch_stub(monkeypatch)
    _block_import(monkeypatch, "transformers")
    backend = TransformersBackend()

    with pytest.raises(ModelLoadException, match="transformers is required"):
        backend.load("models/7b", QUANT_GPTQ_INT4)


def test_transformers_backend_load_maps_oom_to_model_load_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RuntimeError with 'out of memory' becomes ModelLoadException."""
    transformers_stub = _StubTransformers(
        auto_model=_AutoModelStub(load_error=RuntimeError("CUDA out of memory."))
    )
    _install_transformers_stub(monkeypatch, transformers_stub)
    backend = TransformersBackend()

    with pytest.raises(ModelLoadException, match="CUDA OOM"):
        backend.load("models/7b", QUANT_GPTQ_INT4)


def test_transformers_backend_load_maps_generic_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic RuntimeError during load surfaces as ModelLoadException."""
    transformers_stub = _StubTransformers(
        auto_model=_AutoModelStub(load_error=RuntimeError("weight not found"))
    )
    _install_transformers_stub(monkeypatch, transformers_stub)
    backend = TransformersBackend()

    with pytest.raises(ModelLoadException, match="Failed to load model"):
        backend.load("models/7b", QUANT_GPTQ_INT4)


def test_transformers_backend_load_maps_generic_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-RuntimeError exceptions during load become ModelLoadException."""
    transformers_stub = _StubTransformers(
        auto_model=_AutoModelStub(load_error=ValueError("bad config"))
    )
    _install_transformers_stub(monkeypatch, transformers_stub)
    backend = TransformersBackend()

    with pytest.raises(ModelLoadException, match="Failed to load model"):
        backend.load("models/7b", QUANT_GPTQ_INT4)


def test_transformers_backend_generate_raises_when_not_loaded() -> None:
    """generate() without a loaded model raises AiException."""
    backend = TransformersBackend()

    with pytest.raises(AiException, match="no loaded model"):
        backend.generate("hi", max_new_tokens=4, temperature=0.0, timeout_seconds=1.0)


def test_transformers_backend_generate_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generation exceeding timeout raises InferenceTimeoutException and resets."""
    transformers_stub = _StubTransformers(
        auto_tokenizer=_AutoTokenizerStub(),
        auto_model=_AutoModelStub(generate_sleep_seconds=1.0),
    )
    _install_transformers_stub(monkeypatch, transformers_stub)
    backend = TransformersBackend()
    backend.load("models/7b", QUANT_GPTQ_INT4)

    with pytest.raises(InferenceTimeoutException, match="exceeded"):
        backend.generate(
            "hi",
            max_new_tokens=2,
            temperature=0.0,
            timeout_seconds=0.05,
        )

    assert not backend.is_loaded()


def test_transformers_backend_generate_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful generate() returns a populated GenerateResult."""
    transformers_stub = _StubTransformers(
        auto_tokenizer=_AutoTokenizerStub(),
        auto_model=_AutoModelStub(output_ids=[1, 2, 3, 4]),
    )
    _install_transformers_stub(monkeypatch, transformers_stub)
    backend = TransformersBackend()
    backend.load("models/7b", QUANT_GPTQ_INT4)

    result = backend.generate(
        "prompt",
        max_new_tokens=4,
        temperature=0.0,
        timeout_seconds=5.0,
    )

    assert result.text == "decoded"
    assert result.prompt_tokens == 3
    assert result.completion_tokens == 1  # outputs[0][3:] = [4]
    assert result.latency_ms > 0


def test_transformers_backend_unload_resets_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unload() clears the resident model + path."""
    transformers_stub = _StubTransformers(
        auto_tokenizer=_AutoTokenizerStub(),
        auto_model=_AutoModelStub(),
    )
    _install_transformers_stub(monkeypatch, transformers_stub)
    backend = TransformersBackend()
    backend.load("models/7b", QUANT_GPTQ_INT4)

    backend.unload()

    assert not backend.is_loaded()
    assert backend.loaded_model() is None


# ---------------------------------------------------------------------------
# _is_oom helper tests
# ---------------------------------------------------------------------------


def test_is_oom_detects_out_of_memory_message() -> None:
    """RuntimeError mentioning 'out of memory' is detected as OOM."""
    assert _is_oom(RuntimeError("CUDA out of memory. Tried to allocate 2GiB"))
    assert _is_oom(RuntimeError("cuda oom"))


def test_is_oom_returns_false_for_other_errors() -> None:
    """Generic runtime errors are not flagged as OOM."""
    assert not _is_oom(RuntimeError("weight not found"))
    assert not _is_oom(ValueError("bad config"))


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
        json={"model": "7b", "quant": "gptq-int4"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"model": "7b", "quant": "gptq-int4", "loaded": True}
    assert backend.load_calls[0][1] == "gptq-int4"


def test_internal_models_status_endpoint_reports_loaded() -> None:
    """GET /internal/models/status reflects loaded state + scene map."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    hub.load_llm("7b", "gptq-int4")
    client = _client_with_hub(hub)

    response = client.get("/internal/models/status")

    assert response.status_code == 200
    body = response.json()
    assert body["loaded_llm"] == "7b"
    assert body["is_loaded"] is True
    assert body["scenes"]["reason"]["quant"] == "gptq-int4"
    assert body["scenes"]["extract"]["model"] == "1.5b"


def test_internal_models_generate_endpoint() -> None:
    """POST /internal/models/generate returns text + timings."""
    backend = _FakeBackend()
    hub = ModelHub(llm_loader=LlmLoader(backend=backend))
    hub.load_llm("7b", "gptq-int4")
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
    hub.load_llm("7b", "gptq-int4")
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
    hub.load_llm("7b", "gptq-int4")
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
        json={"model": "7b", "quant": "gptq-int4"},
    )

    assert response.status_code == 500
    assert "missing weights" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Stubs for transformers + torch
# ---------------------------------------------------------------------------


class _StubBitsAndBytesConfig:
    """Captures BitsAndBytesConfig kwargs for assertions."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _StubTokenizer:
    """Pretends to be a HF tokenizer."""

    def __call__(self, prompt: str, return_tensors: str = "pt") -> dict[str, Any]:
        """Return a fake input_ids tensor-shaped object."""
        del return_tensors
        ids = list(prompt.encode("utf-8"))[:3] or [1, 2, 3]
        return {"input_ids": _StubTensor(ids)}

    def decode(self, ids: Any, skip_special_tokens: bool = True) -> str:
        """Return a constant decoded string."""
        del skip_special_tokens
        del ids
        return "decoded"


class _StubTensor:
    """Mimics the slice/shape API of a tensor without torch.

    The stub models a 2D tensor of shape ``(1, len(ids))`` initially; row
    indexing ``tensor[0]`` collapses to a 1D row tensor, and slicing returns a
    1D slice — mirroring how the real code uses ``input_ids.shape[1]`` and
    ``outputs[0][prompt_tokens:].shape[0]``.
    """

    def __init__(self, ids: list[int], *, is_2d: bool = True) -> None:
        self._ids = ids
        self._is_2d = is_2d

    @property
    def shape(self) -> tuple[int, ...]:
        """Return shape: ``(1, n)`` for 2D, ``(n,)`` for 1D."""
        if self._is_2d:
            return (1, len(self._ids))
        return (len(self._ids),)

    def __getitem__(self, idx: Any) -> Any:
        """Slice into the underlying ids; preserve _StubTensor wrapping."""
        if isinstance(idx, slice):
            return _StubTensor(self._ids[idx], is_2d=False)
        if isinstance(idx, int):
            if idx == 0 and self._is_2d:
                return _StubTensor(self._ids, is_2d=False)
            return _StubTensor([self._ids[idx]], is_2d=False)
        return _StubTensor(self._ids, is_2d=False)

    def to(self, device: Any) -> "_StubTensor":
        """Pretend to move to a device; return self."""
        del device
        return self

    def __iter__(self) -> Any:
        """Iterate underlying ids."""
        return iter(self._ids)


class _StubModel:
    """Pretends to be a HF causal LM."""

    def __init__(
        self,
        *,
        output_ids: list[int] | None = None,
        generate_sleep_seconds: float = 0.0,
        load_error: Exception | None = None,
    ) -> None:
        self._output_ids = output_ids or [1, 2, 3, 4]
        self._generate_sleep_seconds = generate_sleep_seconds
        self._load_error = load_error

    def load(self) -> None:
        """Raise the configured load error if any."""
        if self._load_error is not None:
            raise self._load_error

    def parameters(self) -> Any:
        """Yield a dummy parameter so device lookup works."""

        class _Param:
            device = "cpu"

        return iter([_Param()])

    def generate(self, **kwargs: Any) -> Any:
        """Sleep then return a fake output tensor."""
        del kwargs
        import time

        if self._generate_sleep_seconds > 0:
            time.sleep(self._generate_sleep_seconds)
        return _StubTensor(self._output_ids)


class _AutoModelStub:
    """Stand-in for transformers.AutoModelForCausalLM."""

    def __init__(
        self,
        *,
        load_error: Exception | None = None,
        output_ids: list[int] | None = None,
        generate_sleep_seconds: float = 0.0,
    ) -> None:
        self._load_error = load_error
        self._output_ids = output_ids
        self._generate_sleep_seconds = generate_sleep_seconds
        self.last_model: _StubModel | None = None

    def from_pretrained(self, *args: Any, **kwargs: Any) -> _StubModel:
        """Return a stub model; raise load_error if set."""
        del args, kwargs
        if self._load_error is not None:
            raise self._load_error
        self.last_model = _StubModel(
            output_ids=self._output_ids,
            generate_sleep_seconds=self._generate_sleep_seconds,
        )
        return self.last_model


class _AutoTokenizerStub:
    """Stand-in for transformers.AutoTokenizer."""

    def from_pretrained(self, *args: Any, **kwargs: Any) -> _StubTokenizer:
        """Return a stub tokenizer."""
        del args, kwargs
        return _StubTokenizer()


class _StubTransformers:
    """Minimal stub of the transformers module surface we use."""

    def __init__(
        self,
        *,
        auto_model: _AutoModelStub | None = None,
        auto_tokenizer: _AutoTokenizerStub | None = None,
    ) -> None:
        self.AutoModelForCausalLM = auto_model or _AutoModelStub()
        self.AutoTokenizer = auto_tokenizer or _AutoTokenizerStub()
        self.BitsAndBytesConfig = _StubBitsAndBytesConfig


def _block_import(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``import <name>`` raise ImportError."""

    import builtins

    real_import = builtins.__import__

    def fake_import(mod: str, *args: Any, **kwargs: Any) -> Any:
        if mod == name:
            raise ImportError(f"blocked {name}")
        return real_import(mod, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _install_torch_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a stub torch module into sys.modules.

    Exposes ``float16``, ``bfloat16``, ``no_grad`` and ``cuda`` so the
    backend's load()/generate() paths work without a real torch install.
    """
    import sys
    import types

    torch_stub = types.ModuleType("torch")
    torch_stub.float16 = "float16"
    torch_stub.bfloat16 = "bfloat16"

    class _Cuda:
        @staticmethod
        def is_available() -> bool:
            return False

        @staticmethod
        def empty_cache() -> None:
            return None

    torch_stub.cuda = _Cuda()

    class _NoGrad:
        def __enter__(self) -> "_NoGrad":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    torch_stub.no_grad = _NoGrad

    monkeypatch.setitem(sys.modules, "torch", torch_stub)


def _install_transformers_stub(
    monkeypatch: pytest.MonkeyPatch, stub: _StubTransformers
) -> None:
    """Inject a stub transformers module into sys.modules.

    Also installs a stub torch module that exposes ``float16``, ``no_grad``,
    ``cuda`` and ``bfloat16`` so the backend's load()/generate() paths work.
    """
    import sys

    _install_torch_stub(monkeypatch)
    monkeypatch.setitem(sys.modules, "transformers", stub)
