"""M2.04 ModelHub HTTP endpoints: /internal/models/{load,status,generate,unload}."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.modules.modelhub import ModelHub, get_modelhub
from app.schemas.models import (
    GenerateRequest,
    GenerateResponse,
    LoadModelRequest,
    LoadModelResponse,
    ModelStatusResponse,
    UnloadResponse,
)
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)
router = APIRouter(tags=["modelhub"])


def get_modelhub_dep() -> ModelHub:
    """Provide the process-wide ModelHub (FastAPI dependency).

    Returns:
        The singleton ModelHub instance.
    """
    return get_modelhub()


@router.post(
    "/internal/models/load",
    response_model=LoadModelResponse,
)
async def load_model(
    request: LoadModelRequest,
    hub: ModelHub = Depends(get_modelhub_dep),
) -> LoadModelResponse:
    """Load an LLM by logical name and quantization.

    Args:
        request: Load request body.
        hub: Injected ModelHub singleton.

    Returns:
        A LoadModelResponse confirming the loaded model key + quant.
    """
    LOGGER.info(
        "[load_model] model=%s quant=%s",
        request.model,
        request.quant,
    )
    hub.load_llm(request.model, request.quant)
    return LoadModelResponse(model=request.model, quant=request.quant)


@router.get(
    "/internal/models/status",
    response_model=ModelStatusResponse,
)
async def model_status(
    hub: ModelHub = Depends(get_modelhub_dep),
) -> ModelStatusResponse:
    """Return the current ModelHub state.

    Args:
        hub: Injected ModelHub singleton.

    Returns:
        A ModelStatusResponse with loaded LLM key + scene routing.
    """
    status = hub.status()
    return ModelStatusResponse(
        loaded_llm=status["loaded_llm"],
        is_loaded=status["is_loaded"],
        scenes=status["scenes"],
    )


@router.post(
    "/internal/models/generate",
    response_model=GenerateResponse,
)
async def generate(
    request: GenerateRequest,
    hub: ModelHub = Depends(get_modelhub_dep),
) -> GenerateResponse:
    """Run a single generate() call on the resident LLM.

    Args:
        request: Generate request body.
        hub: Injected ModelHub singleton.

    Returns:
        A GenerateResponse with text and timings.
    """
    result = hub.generate(
        request.prompt,
        max_new_tokens=request.max_new_tokens,
        temperature=request.temperature,
        timeout_seconds=request.timeout_seconds,
    )
    return GenerateResponse(
        text=result.text,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=result.latency_ms,
        first_token_ms=result.first_token_ms,
    )


@router.post(
    "/internal/models/unload",
    response_model=UnloadResponse,
)
async def unload_model(
    hub: ModelHub = Depends(get_modelhub_dep),
) -> UnloadResponse:
    """Unload the resident LLM and free device memory.

    Args:
        hub: Injected ModelHub singleton.

    Returns:
        An UnloadResponse with the post-unload status.
    """
    was_loaded = hub.llm_loader.is_loaded()
    hub.unload()
    return UnloadResponse(unloaded=was_loaded, status=hub.status())
