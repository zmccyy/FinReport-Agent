"""M4.07 BgeSmallEmbedder: local CPU bge-small-zh-v1.5 embedding engine.

spec §4.3（2026-08-16 API 化后，本地推理栈仅剩 embedding）：
- 模型：``models/bge-small-zh-v1.5``（~95MB，``scripts/download_models.py`` 下载）
- 推理：``SentenceTransformer(model_path, device="cpu")``，惰性加载进程内单例
- 输出：``encode(..., normalize_embeddings=True, batch_size=32)`` → 512 维归一化向量
  （归一化是 Milvus ``fin_kb`` collection ``metric_type=IP`` 的前提，不可关闭）

本模块是 AGENTS.md §8.2 允许触达 transformers 系依赖的唯一入口：
``sentence_transformers`` 在 ``load()`` 内延迟 import——依赖缺失不影响进程
启动与其它 ModelHub 路由，首次 embed 时才 fail loud。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from app.core.config import Settings
from app.core.exceptions import AiException, ModelLoadException
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# spec §4.3：与 Milvus fin_kb collection dim=512 匹配，不可更改。
EMBED_DIM = 512
# spec §4.3：encode batch_size=32。
EMBED_BATCH_SIZE = 32
# 归一化校验容差（float32 精度下 ||v|| 与 1 的偏差上限）。
_NORM_TOLERANCE = 1e-3


class EmbeddingEncoder(Protocol):
    """Minimal encoder contract satisfied by ``SentenceTransformer``."""

    def encode(
        self,
        texts: list[str],
        *,
        normalize_embeddings: bool,
        batch_size: int,
        show_progress_bar: bool,
    ) -> Any:
        """Encode ``texts`` into a row-major array-like (has ``tolist()``).

        Args:
            texts: Input strings.
            normalize_embeddings: L2-normalize each output row.
            batch_size: Encoder batch size.
            show_progress_bar: Progress bar toggle.

        Returns:
            Array-like output with one row per input text.
        """
        ...


EncoderFactory = Callable[[str], EmbeddingEncoder]


def _default_encoder_factory(model_path: str) -> EmbeddingEncoder:
    """Build the production encoder (bge-small-zh-v1.5 on CPU, spec §4.3).

    Args:
        model_path: Local model directory (``Settings.model_embed_path``).

    Returns:
        A loaded SentenceTransformer encoder.

    Raises:
        ModelLoadException: When sentence-transformers is not installed or
            the model fails to initialize.
    """
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise ModelLoadException(
            "sentence-transformers is not installed; install prod extras "
            "(pip install -e '.[prod]') to enable embed()"
        ) from exc
    try:
        return SentenceTransformer(model_path, device="cpu")
    except Exception as exc:
        raise ModelLoadException(
            f"Failed to load bge-small-zh-v1.5 from {model_path!r}: {exc}"
        ) from exc


class BgeSmallEmbedder:
    """Lazy CPU embedder for bge-small-zh-v1.5 (spec §4.3).

    惰性加载：构造不触碰模型目录与 sentence_transformers；首次
    ``embed()``（或显式 ``load()``）才载入并进程内复用。加载与推理共用
    一把互斥锁——CPU 推理本身串行，互斥同时挡住并发首次加载竞态。
    """

    MODEL_KEY = "bge-small-zh-v1.5"

    def __init__(
        self,
        settings: Settings | None = None,
        encoder_factory: EncoderFactory | None = None,
    ) -> None:
        """Configure the embedder.

        Args:
            settings: Application settings (``model_embed_path`` drives the
                model directory).
            encoder_factory: Optional factory override for tests; defaults
                to ``SentenceTransformer(path, device="cpu")``.
        """
        self.settings = settings or Settings()
        self._encoder_factory = encoder_factory or _default_encoder_factory
        self._encoder: EmbeddingEncoder | None = None
        self._lock = threading.Lock()

    def load(self) -> None:
        """Load the encoder (idempotent; re-call after failure retries).

        Raises:
            ModelLoadException: When the model directory is missing, the
                dependency is absent, or initialization fails.
        """
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        """Load without acquiring the lock (caller must hold ``self._lock``)."""
        if self._encoder is not None:
            return
        model_path = self.settings.model_embed_path
        if not Path(model_path).is_dir():
            raise ModelLoadException(
                f"Embedding model directory not found: {model_path!r}; "
                "run `python scripts/download_models.py --model bge` first"
            )
        LOGGER.info("[BgeSmallEmbedder.load] path=%s device=cpu", model_path)
        try:
            self._encoder = self._encoder_factory(model_path)
        except ModelLoadException:
            raise
        except Exception as exc:
            raise ModelLoadException(
                f"Embedding engine failed to initialize from {model_path!r}: {exc}"
            ) from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts into 512-dim L2-normalized vectors (spec §4.3).

        Args:
            texts: Texts to embed (KB chunks / queries).

        Returns:
            One 512-dim normalized float vector per input text; an empty
            input returns an empty list without loading the model.

        Raises:
            AiException: When inputs are not strings, inference fails, or
                the output violates the 512-dim normalized contract.
            ModelLoadException: When the model cannot be loaded.
        """
        if not texts:
            return []
        if not all(isinstance(t, str) for t in texts):
            raise AiException("embed() expects a list of str texts")
        with self._lock:
            self._load_locked()
            encoder = self._encoder
            assert encoder is not None  # locked invariant: load ran above
            try:
                raw = encoder.encode(
                    texts,
                    normalize_embeddings=True,
                    batch_size=EMBED_BATCH_SIZE,
                    show_progress_bar=False,
                )
            except AiException:
                raise
            except Exception as exc:
                raise AiException(f"Embedding inference failed: {exc}") from exc
        vectors = self._to_float_rows(raw, len(texts))
        self._validate_contract(vectors)
        return vectors

    @staticmethod
    def _to_float_rows(raw: Any, expected_count: int) -> list[list[float]]:
        """Normalize encoder output into plain float rows.

        Args:
            raw: Encoder output (ndarray-like with ``tolist()`` or a nested
                list — the latter keeps unit tests numpy-free).
            expected_count: Number of input texts.

        Returns:
            A list of float rows.

        Raises:
            AiException: When the output shape is unusable or the row count
                does not match the input.
        """
        try:
            rows = raw.tolist() if hasattr(raw, "tolist") else raw
            converted = [[float(x) for x in row] for row in rows]
        except (AttributeError, TypeError, ValueError) as exc:
            raise AiException(f"Unusable encoder output shape: {exc}") from exc
        if len(converted) != expected_count:
            raise AiException(
                f"Embedding row count mismatch: got {len(converted)}, expected {expected_count}"
            )
        return converted

    @staticmethod
    def _validate_contract(vectors: list[list[float]]) -> None:
        """Enforce the 512-dim L2-normalized contract (Milvus IP metric).

        Args:
            vectors: Candidate vectors.

        Raises:
            AiException: On dimension mismatch or a non-normalized vector.
        """
        for i, vec in enumerate(vectors):
            if len(vec) != EMBED_DIM:
                raise AiException(
                    f"Embedding dim mismatch at index {i}: got {len(vec)}, "
                    f"expected {EMBED_DIM} (Milvus fin_kb dim=512)"
                )
            norm = math.sqrt(sum(x * x for x in vec))
            if abs(norm - 1.0) > _NORM_TOLERANCE:
                raise AiException(
                    f"Embedding vector at index {i} is not L2-normalized "
                    f"(norm={norm:.6f}); Milvus IP metric requires "
                    "normalized vectors"
                )

    def is_loaded(self) -> bool:
        """Return True when the encoder is resident."""
        with self._lock:
            return self._encoder is not None

    def loaded_model(self) -> str | None:
        """Return the model key when loaded, else None."""
        with self._lock:
            return self.MODEL_KEY if self._encoder is not None else None
