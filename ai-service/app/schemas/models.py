"""HTTP schemas for the M2.04 ModelHub endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LoadModelRequest(BaseModel):
    """Request body for ``POST /internal/models/load``."""

    model: str = Field(
        description="Logical model key (e.g. ``7b`` / ``1.5b``)",
        examples=["7b"],
    )
    quant: str = Field(
        description="Quantization label (``gptq-int4`` / ``nf4``)",
        examples=["gptq-int4"],
    )


class LoadModelResponse(BaseModel):
    """Response body for ``POST /internal/models/load``."""

    model: str
    quant: str
    loaded: bool = True


class GenerateRequest(BaseModel):
    """Request body for ``POST /internal/models/generate``."""

    prompt: str = Field(min_length=1)
    max_new_tokens: int = Field(default=512, ge=1, le=4096)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_seconds: float | None = Field(default=None, ge=1.0, le=600.0)


class GenerateResponse(BaseModel):
    """Response body for ``POST /internal/models/generate``."""

    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    first_token_ms: float


class ModelStatusResponse(BaseModel):
    """Response body for ``GET /internal/models/status``."""

    loaded_llm: str | None
    is_loaded: bool
    scenes: dict[str, dict[str, str]]


class UnloadResponse(BaseModel):
    """Response body for ``POST /internal/models/unload``."""

    unloaded: bool
    status: dict[str, Any]
