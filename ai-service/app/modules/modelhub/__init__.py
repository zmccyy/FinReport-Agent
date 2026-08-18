"""M11 ModelHub package: API inference routing (M4.08: GPU 栈已移除)."""

from app.modules.modelhub.api_backend import QUANT_API, DeepSeekBackend
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
    "LLM_SCENES",
    "QUANT_API",
    "SCENE_MODEL_MAP",
    "DeepSeekBackend",
    "GenerateResult",
    "LlmBackend",
    "LlmLoader",
    "ModelHub",
    "Scene",
    "get_modelhub",
    "reset_modelhub",
]
