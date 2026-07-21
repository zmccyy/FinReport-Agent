"""M11 ModelHub package: model loading + inference routing."""

from app.modules.modelhub.llm_loader import (
    QUANT_GPTQ_INT4,
    QUANT_NF4,
    GenerateResult,
    LlmBackend,
    LlmLoader,
    TransformersBackend,
)
from app.modules.modelhub.modelhub import (
    LLM_SCENES,
    SCENE_MODEL_MAP,
    ModelHub,
    Scene,
    get_modelhub,
    reset_modelhub,
)
from app.modules.modelhub.vram_scheduler import (
    MODEL_LOCK_KEY_PREFIX,
    MODEL_LOCK_TTL_SECONDS_DEFAULT,
    ModelLock,
    VramScheduler,
)

__all__ = [
    "LLM_SCENES",
    "MODEL_LOCK_KEY_PREFIX",
    "MODEL_LOCK_TTL_SECONDS_DEFAULT",
    "QUANT_GPTQ_INT4",
    "QUANT_NF4",
    "SCENE_MODEL_MAP",
    "GenerateResult",
    "LlmBackend",
    "LlmLoader",
    "ModelHub",
    "ModelLock",
    "Scene",
    "TransformersBackend",
    "VramScheduler",
    "get_modelhub",
    "reset_modelhub",
]
