"""M2.04 ModelHub: scene-routed model loading and inference entrypoint.

Spec §3.5 routes inference by scene:
- ``EXTRACT``  → Qwen2.5-1.5B QLoRA  (本地, 4-bit)  — M2.06+ (T1 adapter)
- ``REASON``   → Qwen2.5-7B-Instruct (本地, 4-bit GPTQ)
- ``EMBED``    → bge-small-zh LoRA    (本地)         — M5+
- ``LAYOUT``   → LayoutLMv3           (本地)         — M4+

M2.04 only needs the 7B REASON path. Other scenes return their configured
model_key/quant pair but ``load_llm`` only supports the 7B and 1.5B LLM keys
in this iteration; embed/layout will be wired in their respective milestones.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AiException, ModelLoadException
from app.modules.modelhub.llm_loader import (
    QUANT_GPTQ_INT4,
    QUANT_NF4,
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
    LAYOUT = "layout"


# Logical model key + quant label per scene.
SCENE_MODEL_MAP: dict[Scene, tuple[str, str]] = {
    Scene.EXTRACT: ("1.5b", QUANT_NF4),
    Scene.REASON: ("7b", QUANT_GPTQ_INT4),
    Scene.EMBED: ("bge", "lora"),
    Scene.LAYOUT: ("layoutlm", "fp16"),
}

# LLM scenes (routed through LlmLoader). EMBED/LAYOUT use their own engines
# in later milestones and are not loadable as LLMs here.
LLM_SCENES: frozenset[Scene] = frozenset({Scene.EXTRACT, Scene.REASON})


class ModelHub:
    """Unified model loading/inference entrypoint (spec §2.3 M11)."""

    def __init__(
        self,
        settings: Settings | None = None,
        llm_loader: LlmLoader | None = None,
    ) -> None:
        """Configure the ModelHub.

        Args:
            settings: Application settings (model paths + SLA knobs).
            llm_loader: Optional LlmLoader override for tests.
        """
        self.settings = settings or Settings()
        self.llm_loader = llm_loader or LlmLoader(self.settings)

    def load_llm(self, name: str, quant: str) -> None:
        """Load an LLM by logical name and quantization.

        Args:
            name: Logical model key (``"7b"`` / ``"1.5b"``).
            quant: Quantization label (``"gptq-int4"`` / ``"nf4"``).

        Raises:
            ModelLoadException: When ``name`` is unknown or the backend fails
                to load.
        """
        path = self._resolve_model_path(name)
        LOGGER.info(
            "[ModelHub.load_llm] name=%s quant=%s path=%s",
            name,
            quant,
            path,
        )
        self.llm_loader.load(name, path, quant)

    def generate(
        self,
        prompt: str,
        *,
        max_new_tokens: int | None = None,
        temperature: float = 0.0,
        timeout_seconds: float | None = None,
    ) -> GenerateResult:
        """Generate a completion on the resident LLM.

        Args:
            prompt: Input prompt.
            max_new_tokens: Override default max tokens.
            temperature: Sampling temperature; 0 means greedy.
            timeout_seconds: Override SLA timeout.

        Returns:
            A GenerateResult.

        Raises:
            AiException: When no LLM is loaded.
            InferenceTimeoutException: On timeout.
        """
        return self.llm_loader.generate(
            prompt,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts (stub — wired in M5 with bge-small-zh LoRA).

        Args:
            texts: Texts to embed.

        Returns:
            One float vector per input text.

        Raises:
            AiException: Always in M2.04 — embedder lands in M5.
        """
        del texts
        raise AiException(
            "ModelHub.embed() is not implemented in M2.04; bge-small-zh LoRA embedder lands in M5"
        )

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
                (EMBED/LAYOUT will be wired in their own milestones).
            ModelLoadException: When the backend fails to load.
        """
        if scene not in LLM_SCENES:
            raise AiException(
                f"Scene {scene.value} does not route through LlmLoader in M2.04"
            )
        model_key, quant = self.route(scene)
        self.load_llm(model_key, quant)

    def unload(self) -> None:
        """Unload the resident LLM."""
        self.llm_loader.unload()

    def status(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the ModelHub state.

        Returns:
            A dict with the loaded LLM key and per-scene routing.
        """
        loaded = self.llm_loader.loaded_model()
        return {
            "loaded_llm": loaded,
            "is_loaded": loaded is not None,
            "scenes": {
                scene.value: {"model": k, "quant": q}
                for scene, (k, q) in SCENE_MODEL_MAP.items()
            },
        }

    def _resolve_model_path(self, name: str) -> str:
        """Resolve a logical model key to a filesystem path.

        Args:
            name: Logical model key (``"7b"`` / ``"1.5b"`` / ``"bge"`` / ``"layoutlm"``).

        Returns:
            Absolute or relative path to the model directory.

        Raises:
            ModelLoadException: When the key is unknown.
        """
        mapping = {
            "7b": self.settings.model_7b_path,
            "1.5b": self.settings.model_15b_path,
            "bge": self.settings.model_embed_path,
            "layoutlm": "models/layoutlmv3-base",
        }
        if name not in mapping:
            raise ModelLoadException(f"Unknown model name: {name}")
        path = mapping[name]
        return str(Path(path).expanduser())


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
