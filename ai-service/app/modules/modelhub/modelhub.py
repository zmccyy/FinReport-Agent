"""ModelHub: scene-routed inference entrypoint (M2.04 + M4.02/M4.07 改造).

Spec §3.5 routes inference by scene（2026-08-16 变更，本地训练取消）：
- ``EXTRACT``  → DeepSeek API deepseek-chat（json_mode）
- ``REASON``   → DeepSeek API deepseek-chat
- ``EMBED``    → bge-small-zh-v1.5（本地 CPU，512 维，M4.07 实装）

M4.08 起唯一 LLM 后端为 ``DeepSeekBackend``（本地 TransformersBackend
与遗留 ``7b``/``1.5b`` 键已随 GPU 栈移除）；``llm_backend`` 参数仅供
单测注入 fake。Embedding 由 ``BgeSmallEmbedder`` 承担（spec §4.3），
``embedder`` 参数同样仅供单测注入 fake。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AiException, ModelLoadException
from app.modules.modelhub.api_backend import QUANT_API, DeepSeekBackend
from app.modules.modelhub.embedder import EMBED_DIM, BgeSmallEmbedder
from app.modules.modelhub.llm_loader import (
    GenerateResult,
    LlmBackend,
    LlmLoader,
)
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)


class Scene(str, Enum):
    """Inference scene labels routed by ModelHub (spec §3.5)."""

    EXTRACT = "extract"
    REASON = "reason"
    EMBED = "embed"


# Logical model key + quant label per scene (2026-08-16 API 化).
SCENE_MODEL_MAP: dict[Scene, tuple[str, str]] = {
    Scene.EXTRACT: ("deepseek-chat", QUANT_API),
    Scene.REASON: ("deepseek-chat", QUANT_API),
    Scene.EMBED: ("bge", "cpu"),
}

# LLM scenes (routed through the API backend). EMBED uses its own engine
# (M4.07) and is not loadable as an LLM here.
LLM_SCENES: frozenset[Scene] = frozenset({Scene.EXTRACT, Scene.REASON})


class ModelHub:
    """Unified model loading/inference entrypoint (spec §2.3 M11)."""

    def __init__(
        self,
        settings: Settings | None = None,
        llm_loader: LlmLoader | None = None,
        llm_backend: LlmBackend | None = None,
        embedder: BgeSmallEmbedder | None = None,
    ) -> None:
        """Configure the ModelHub.

        Args:
            settings: Application settings (API config + SLA knobs).
            llm_loader: Optional LlmLoader override for tests (defaults to
                a loader wrapping the DeepSeek API backend).
            llm_backend: Optional backend used to build the default loader
                (ignored when ``llm_loader`` is provided).
            embedder: Optional BgeSmallEmbedder override for tests (defaults
                to a lazily-loading bge-small-zh-v1.5 engine, spec §4.3).
        """
        self.settings = settings or Settings()
        if llm_loader is None:
            backend = llm_backend or DeepSeekBackend(self.settings)
            llm_loader = LlmLoader(self.settings, backend=backend)
        self.llm_loader = llm_loader
        self.embedder = embedder or BgeSmallEmbedder(self.settings)

    def load_llm(self, name: str, quant: str) -> None:
        """Load an LLM by logical name and routing label.

        Args:
            name: Logical model key (``"deepseek-chat"``).
            quant: Routing label; M4.08 起仅支持 ``"api"``。

        Raises:
            ModelLoadException: When ``quant`` is not the API label or
                the backend fails to load.
        """
        if quant != QUANT_API:
            raise ModelLoadException(
                f"Unsupported quant={quant!r}: the local GPU stack was "
                "removed in M4.08; only the DeepSeek API route remains"
            )
        LOGGER.info("[ModelHub.load_llm] name=%s quant=%s", name, quant)
        self.llm_loader.load(name, "", quant)

    def generate(
        self,
        prompt: str,
        *,
        max_new_tokens: int | None = None,
        temperature: float = 0.0,
        timeout_seconds: float | None = None,
        system_prompt: str | None = None,
        json_mode: bool = False,
    ) -> GenerateResult:
        """Generate a completion (DeepSeek API by default, M4.02).

        The default API backend needs no explicit ``load_llm()`` — the call
        auto-initializes the HTTP client on first use.

        Args:
            prompt: Input prompt.
            max_new_tokens: Override default max tokens.
            temperature: Sampling temperature; 0 means greedy.
            timeout_seconds: Override SLA timeout.
            system_prompt: Optional system message (API backends).
            json_mode: Request JSON-constrained output (API backends).

        Returns:
            A GenerateResult.

        Raises:
            AiException: On 4xx API errors or retries exhausted.
            InferenceTimeoutException: On API timeout.
            ModelLoadException: When the API key is not configured.
        """
        if not self.llm_loader.is_loaded():
            # API 模式：默认后端无需显式加载，首次调用自动初始化。
            self.load_llm(self.settings.llm_api_model, QUANT_API)
        return self.llm_loader.generate(
            prompt,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            system_prompt=system_prompt,
            json_mode=json_mode,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts (bge-small-zh-v1.5, spec §4.3, M4.07).

        引擎惰性加载：首次调用触发模型载入（进程内复用），空输入不触
        发加载。归一化与 512 维校验由 BgeSmallEmbedder 强制执行——这是
        Milvus ``fin_kb``（dim=512, metric=IP）检索语义的前提。

        Args:
            texts: Texts to embed (KB chunks / queries).

        Returns:
            One 512-dim L2-normalized float vector per input text.

        Raises:
            AiException: When inputs are invalid, inference fails, or the
                output violates the 512-dim normalized contract.
            ModelLoadException: When the model cannot be loaded.
        """
        return self.embedder.embed(texts)

    def route(self, scene: Scene) -> tuple[str, str]:
        """Return the (model_key, quant) pair for a scene.

        Args:
            scene: Inference scene.

        Returns:
            A tuple of (model_key, quant_label).

        Raises:
            KeyError: When the scene is not in ``SCENE_MODEL_MAP``.
        """
        return SCENE_MODEL_MAP[scene]

    def load_for_scene(self, scene: Scene) -> None:
        """Convenience: load the LLM required by ``scene``.

        Args:
            scene: Inference scene.

        Raises:
            AiException: When the scene does not route through the LLM loader
                (EMBED uses BgeSmallEmbedder, M4.07).
            ModelLoadException: When the backend fails to load.
        """
        if scene not in LLM_SCENES:
            raise AiException(f"Scene {scene.value} does not route through the LLM backend")
        model_key, quant = self.route(scene)
        self.load_llm(model_key, quant)

    def unload(self) -> None:
        """Unload the resident LLM."""
        self.llm_loader.unload()

    def status(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the ModelHub state.

        Returns:
            A dict with the loaded LLM key, per-scene routing, and the
            embed engine state (model / dim / loaded).
        """
        loaded = self.llm_loader.loaded_model()
        return {
            "loaded_llm": loaded,
            "is_loaded": loaded is not None,
            "scenes": {
                scene.value: {"model": k, "quant": q} for scene, (k, q) in SCENE_MODEL_MAP.items()
            },
            "embed": {
                "model": self.embedder.MODEL_KEY,
                "dim": EMBED_DIM,
                "is_loaded": self.embedder.is_loaded(),
            },
        }


_DEFAULT_HUB: ModelHub | None = None


def get_modelhub() -> ModelHub:
    """Return the process-wide ModelHub singleton.

    Returns:
        A shared ModelHub instance. Built lazily on first call.
    """
    global _DEFAULT_HUB
    if _DEFAULT_HUB is None:
        _DEFAULT_HUB = ModelHub()
    return _DEFAULT_HUB


def reset_modelhub(hub: ModelHub | None = None) -> None:
    """Reset the singleton (test helper).

    Args:
        hub: Optional override; pass ``None`` to clear.
    """
    global _DEFAULT_HUB
    _DEFAULT_HUB = hub


__all__ = [
    "GenerateResult",
    "LlmBackend",
    "LlmLoader",
    "ModelHub",
    "Scene",
    "get_modelhub",
    "reset_modelhub",
]
