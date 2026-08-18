"""M4.07 BgeSmallEmbedder + ModelHub.embed() tests (512-dim, L2-normalized).

All tests inject a fake encoder factory — the real bge-small-zh-v1.5 weights
(~95MB) are a deployment concern (M4.09/M4.10); here we pin the contract:
512 dims, unit L2 norm, batch_size=32, normalize always on, lazy singleton
loading, and fail-loud error mapping (spec §4.3).
"""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import AiException, ModelLoadException
from app.modules.modelhub import (
    EMBED_BATCH_SIZE,
    EMBED_DIM,
    BgeSmallEmbedder,
    ModelHub,
)
from app.modules.modelhub.embedder import _default_encoder_factory
from app.modules.modelhub.llm_loader import LlmLoader


def _unit_vector(dim: int = EMBED_DIM, scale: float = 1.0) -> list[float]:
    """Build a `dim`-vector whose L2 norm is `scale`."""
    comp = scale / math.sqrt(dim)
    return [comp] * dim


class _FakeEncoder:
    """Mimics SentenceTransformer.encode() with canned vector rows."""

    def __init__(
        self,
        template: list[float],
        *,
        error: Exception | None = None,
        rows_override: list[list[float]] | None = None,
    ) -> None:
        self.template = template
        self.error = error
        self.rows_override = rows_override
        self.encode_calls: list[dict[str, Any]] = []

    def encode(
        self,
        texts: list[str],
        *,
        normalize_embeddings: bool,
        batch_size: int,
        show_progress_bar: bool,
    ) -> list[list[float]]:
        """Return one canned row per input text (or the fixed override)."""
        self.encode_calls.append(
            {
                "texts": list(texts),
                "normalize_embeddings": normalize_embeddings,
                "batch_size": batch_size,
                "show_progress_bar": show_progress_bar,
            }
        )
        if self.error is not None:
            raise self.error
        if self.rows_override is not None:
            return [list(row) for row in self.rows_override]
        return [list(self.template) for _ in texts]


class _RecordingFactory:
    """Encoder factory that records paths and builds _FakeEncoder instances."""

    def __init__(self, encoder: _FakeEncoder, *, error: Exception | None = None) -> None:
        self.encoder = encoder
        self.error = error
        self.factory_calls: list[str] = []

    def __call__(self, model_path: str) -> _FakeEncoder:
        self.factory_calls.append(model_path)
        if self.error is not None:
            raise self.error
        return self.encoder


def _make_embedder(
    tmp_path: Path,
    encoder: _FakeEncoder,
    *,
    factory_error: Exception | None = None,
) -> tuple[BgeSmallEmbedder, _RecordingFactory]:
    """Build a BgeSmallEmbedder wired to a fake factory under tmp_path."""
    settings = Settings(model_embed_path=str(tmp_path))
    factory = _RecordingFactory(encoder, error=factory_error)
    return BgeSmallEmbedder(settings=settings, encoder_factory=factory), factory


# ---------------------------------------------------------------------------
# Happy path: contract on vectors + encode kwargs
# ---------------------------------------------------------------------------


def test_embed_returns_512_dim_normalized_vectors(tmp_path: Path) -> None:
    """embed() yields one 512-dim unit-norm vector per input text."""
    encoder = _FakeEncoder(_unit_vector())
    embedder, _ = _make_embedder(tmp_path, encoder)

    vectors = embedder.embed(["营业收入", "净利润"])

    assert len(vectors) == 2
    for vec in vectors:
        assert len(vec) == EMBED_DIM
        assert all(isinstance(x, float) for x in vec)
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 1e-3


def test_embed_forces_spec_encode_kwargs(tmp_path: Path) -> None:
    """encode() is always called with normalization on and batch_size=32."""
    encoder = _FakeEncoder(_unit_vector())
    embedder, _ = _make_embedder(tmp_path, encoder)

    embedder.embed(["text"])

    call = encoder.encode_calls[0]
    assert call["normalize_embeddings"] is True
    assert call["batch_size"] == EMBED_BATCH_SIZE == 32
    assert call["show_progress_bar"] is False
    assert call["texts"] == ["text"]


def test_embed_empty_input_returns_empty_without_loading(tmp_path: Path) -> None:
    """Empty input short-circuits: no factory call, no model load."""
    encoder = _FakeEncoder(_unit_vector())
    embedder, factory = _make_embedder(tmp_path, encoder)

    assert embedder.embed([]) == []
    assert factory.factory_calls == []
    assert embedder.is_loaded() is False


# ---------------------------------------------------------------------------
# Lazy singleton loading
# ---------------------------------------------------------------------------


def test_first_embed_loads_once_and_reuses_encoder(tmp_path: Path) -> None:
    """The factory runs exactly once; later embeds reuse the encoder."""
    encoder = _FakeEncoder(_unit_vector())
    embedder, factory = _make_embedder(tmp_path, encoder)

    embedder.embed(["a"])
    embedder.embed(["b"])

    assert factory.factory_calls == [str(tmp_path)]
    assert len(encoder.encode_calls) == 2
    assert embedder.is_loaded() is True
    assert embedder.loaded_model() == BgeSmallEmbedder.MODEL_KEY


def test_explicit_load_then_embed_skips_factory(tmp_path: Path) -> None:
    """Explicit load() pre-warms the engine; embed() does not reload."""
    encoder = _FakeEncoder(_unit_vector())
    embedder, factory = _make_embedder(tmp_path, encoder)

    embedder.load()
    embedder.embed(["a"])

    assert factory.factory_calls == [str(tmp_path)]


def test_loaded_model_is_none_before_load(tmp_path: Path) -> None:
    """Before any load, loaded_model() is None and is_loaded() is False."""
    encoder = _FakeEncoder(_unit_vector())
    embedder, _ = _make_embedder(tmp_path, encoder)

    assert embedder.is_loaded() is False
    assert embedder.loaded_model() is None


# ---------------------------------------------------------------------------
# Load failure mapping
# ---------------------------------------------------------------------------


def test_load_missing_model_dir_hints_download_script(tmp_path: Path) -> None:
    """A missing model directory raises ModelLoadException with the fix hint."""
    settings = Settings(model_embed_path=str(tmp_path / "bge-small-zh-v1.5"))
    factory = _RecordingFactory(_FakeEncoder(_unit_vector()))
    embedder = BgeSmallEmbedder(settings=settings, encoder_factory=factory)

    with pytest.raises(ModelLoadException, match="download_models.py"):
        embedder.embed(["text"])


def test_factory_error_wrapped_as_model_load_exception(tmp_path: Path) -> None:
    """Any factory failure maps to ModelLoadException."""
    encoder = _FakeEncoder(_unit_vector())
    embedder, _ = _make_embedder(tmp_path, encoder, factory_error=OSError("disk full"))

    with pytest.raises(ModelLoadException, match="disk full"):
        embedder.embed(["text"])
    assert embedder.is_loaded() is False


def test_default_factory_wraps_import_error(tmp_path: Path) -> None:
    """_default_encoder_factory maps ImportError to ModelLoadException."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("sentence_transformers"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _blocked  # type: ignore[assignment]
    try:
        with pytest.raises(ModelLoadException, match="sentence-transformers is not installed"):
            _default_encoder_factory(str(tmp_path))
    finally:
        builtins.__import__ = real_import  # type: ignore[assignment]


def test_default_factory_wraps_model_init_error(tmp_path: Path) -> None:
    """_default_encoder_factory maps init failures to ModelLoadException."""

    class _ExplodingEncoder:
        def __init__(self, path: str, device: str) -> None:
            del path, device
            raise ValueError("corrupt weights")

    import sys
    import types

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = _ExplodingEncoder  # type: ignore[attr-defined]
    monkeypatch_holder = pytest.MonkeyPatch()
    monkeypatch_holder.setitem(sys.modules, "sentence_transformers", fake_module)
    try:
        with pytest.raises(ModelLoadException, match="corrupt weights"):
            _default_encoder_factory(str(tmp_path))
    finally:
        monkeypatch_holder.undo()


# ---------------------------------------------------------------------------
# Inference failure + contract enforcement
# ---------------------------------------------------------------------------


def test_embed_inference_error_wrapped_as_ai_exception(tmp_path: Path) -> None:
    """Encoder.encode() failures map to AiException."""
    encoder = _FakeEncoder(_unit_vector(), error=RuntimeError("backend crashed"))
    embedder, _ = _make_embedder(tmp_path, encoder)

    with pytest.raises(AiException, match="Embedding inference failed"):
        embedder.embed(["text"])


def test_embed_dim_mismatch_rejected(tmp_path: Path) -> None:
    """Non-512-dim output violates the Milvus contract and is rejected."""
    encoder = _FakeEncoder(_unit_vector(dim=128))
    embedder, _ = _make_embedder(tmp_path, encoder)

    with pytest.raises(AiException, match="dim mismatch.*512"):
        embedder.embed(["text"])


def test_embed_unnormalized_vector_rejected(tmp_path: Path) -> None:
    """A vector with norm != 1 breaks IP-metric semantics and is rejected."""
    encoder = _FakeEncoder(_unit_vector(scale=2.0))
    embedder, _ = _make_embedder(tmp_path, encoder)

    with pytest.raises(AiException, match="not L2-normalized"):
        embedder.embed(["text"])


def test_embed_row_count_mismatch_rejected(tmp_path: Path) -> None:
    """Encoder returning fewer rows than inputs is rejected."""
    encoder = _FakeEncoder(
        _unit_vector(),
        rows_override=[_unit_vector()],
    )
    embedder, _ = _make_embedder(tmp_path, encoder)

    with pytest.raises(AiException, match="row count mismatch"):
        embedder.embed(["a", "b"])


def test_embed_non_string_input_rejected(tmp_path: Path) -> None:
    """Non-str entries fail fast before touching the encoder."""
    encoder = _FakeEncoder(_unit_vector())
    embedder, _ = _make_embedder(tmp_path, encoder)

    with pytest.raises(AiException, match="list of str"):
        embedder.embed(["valid", 123])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_first_embed_loads_factory_once(tmp_path: Path) -> None:
    """Concurrent first embeds trigger exactly one factory call."""
    encoder = _FakeEncoder(_unit_vector())
    embedder, factory = _make_embedder(tmp_path, encoder)
    barrier = threading.Barrier(4)

    def _worker() -> None:
        barrier.wait()
        embedder.embed(["text"])

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert factory.factory_calls == [str(tmp_path)]
    assert len(encoder.encode_calls) == 4


# ---------------------------------------------------------------------------
# ModelHub wiring
# ---------------------------------------------------------------------------


def _hub_with_embedder(embedder: BgeSmallEmbedder) -> ModelHub:
    """Build a ModelHub with a fake LLM loader and the given embedder."""
    return ModelHub(llm_loader=LlmLoader(backend=_NoopBackend()), embedder=embedder)


class _NoopBackend:
    """Minimal LlmBackend stub (embed tests never touch the LLM route)."""

    def load(self, model_key: str, quant: str) -> None:
        del model_key, quant

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("LLM generate must not run in embed tests")

    def unload(self) -> None:
        return None

    def is_loaded(self) -> bool:
        return False

    def loaded_model(self) -> str | None:
        return None


def test_modelhub_embed_delegates_to_embedder(tmp_path: Path) -> None:
    """ModelHub.embed() forwards to the wired BgeSmallEmbedder."""
    encoder = _FakeEncoder(_unit_vector())
    embedder, _ = _make_embedder(tmp_path, encoder)
    hub = _hub_with_embedder(embedder)

    vectors = hub.embed(["净资产"])

    assert len(vectors) == 1
    assert len(vectors[0]) == EMBED_DIM
    assert encoder.encode_calls[0]["texts"] == ["净资产"]


def test_modelhub_default_construction_wires_bge_embedder() -> None:
    """Default ModelHub carries a lazily-loaded BgeSmallEmbedder."""
    hub = ModelHub()

    assert isinstance(hub.embedder, BgeSmallEmbedder)
    assert hub.embedder.is_loaded() is False
    assert hub.embedder.loaded_model() is None


def test_modelhub_status_reports_embed_engine(tmp_path: Path) -> None:
    """status() exposes the embed engine model/dim/loaded state."""
    encoder = _FakeEncoder(_unit_vector())
    embedder, _ = _make_embedder(tmp_path, encoder)
    hub = _hub_with_embedder(embedder)

    status = hub.status()
    assert status["embed"] == {
        "model": "bge-small-zh-v1.5",
        "dim": 512,
        "is_loaded": False,
    }

    hub.embed(["text"])
    status = hub.status()
    assert status["embed"]["is_loaded"] is True
