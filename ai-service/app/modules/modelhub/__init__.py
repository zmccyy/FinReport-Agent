"""M11 ModelHub package: API inference + local embedding routing (M4.07)."""

from app.modules.modelhub.api_backend import QUANT_API, DeepSeekBackend
from app.modules.modelhub.embedder import (
    EMBED_BATCH_SIZE,
    EMBED_DIM,
    BgeSmallEmbedder,
)
from app.modules.modelhub.llm_loader import (
    GenerateResult,
    LlmBackend,
    LlmLoader,
)
from app.modules.modelhub.modelhub import (
    LLM_SCENES,
    SCENE_MODEL_MAP,
    ModelHub,
    Scene,
    get_modelhub,
    reset_modelhub,
)

__all__ = [
    "BgeSmallEmbedder",
    "DeepSeekBackend",
    "EMBED_BATCH_SIZE",
    "EMBED_DIM",
    "GenerateResult",
    "LLM_SCENES",
    "LlmBackend",
    "LlmLoader",
    "ModelHub",
    "QUANT_API",
    "SCENE_MODEL_MAP",
    "Scene",
    "get_modelhub",
    "reset_modelhub",
]
