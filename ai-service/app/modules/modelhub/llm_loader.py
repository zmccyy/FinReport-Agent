"""M2.04 LlmLoader: load a 4-bit LLM and expose generate().

The loader keeps a single LLM resident at a time (spec §8.1: 6GB VRAM only
fits one 7B 4-bit OR one 1.5B + one small). Loading a different model first
unloads the previous one. The backend is pluggable so unit tests inject a
fake; production uses ``TransformersBackend`` which lazy-imports torch and
transformers so the service stays importable without the heavy stack.

Supported quantization formats:
- ``gptq-int4``: pre-quantized GPTQ model (e.g. Qwen2.5-7B-Instruct-GPTQ-Int4).
  transformers auto-detects the GPTQ config from ``config.json``; no extra
  ``quantization_config`` is required.
- ``nf4``: bitsandbytes 4-bit NF4 (used for the 1.5B QLoRA base). Builds a
  ``BitsAndBytesConfig`` and lets transformers quantize on load.

Failure modes mapped to ``ModelLoadException``:
- missing torch/transformers/bitsandbytes imports
- CUDA OOM during ``from_pretrained``
- missing model directory or weights
- any other ``RuntimeError`` raised by transformers during load

Inference timeouts are enforced by ``GenerateTimeoutGuard`` and surface as
``InferenceTimeoutException`` so callers can fall back to the API 72B model
(spec §10.3).
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.config import Settings
from app.core.exceptions import (
    AiException,
    InferenceTimeoutException,
    ModelLoadException,
)
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# Quantization labels (kept as plain strings to avoid enum import cycles).
QUANT_GPTQ_INT4 = "gptq-int4"
QUANT_NF4 = "nf4"


@dataclass(frozen=True)
class GenerateResult:
    """Structured output of a single generate() call.

    Attributes:
        text: Decoded model response (prompt stripped).
        prompt_tokens: Number of input tokens.
        completion_tokens: Number of newly generated tokens.
        latency_ms: Wall-clock generation time in milliseconds.
        first_token_ms: Time to first token in milliseconds (best-effort).
    """

    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    first_token_ms: float = 0.0


class LlmBackend(Protocol):
    """Pluggable inference backend contract."""

    def load(self, model_path: str, quant: str) -> None:
        """Load the model identified by ``model_path``.

        Args:
            model_path: Local filesystem path to the model directory.
            quant: Quantization label (gptq-int4 / nf4).

        Raises:
            ModelLoadException: When the model cannot be loaded.
        """
        ...

    def generate(
        self,
        prompt: str,
        *,
        max_new_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> GenerateResult:
        """Run a single generate call against the loaded model.

        Args:
            prompt: Tokenized prompt text.
            max_new_tokens: Maximum tokens to generate.
            temperature: Sampling temperature (0 = greedy).
            timeout_seconds: Inference SLA timeout.

        Returns:
            A GenerateResult carrying the decoded text and timings.

        Raises:
            InferenceTimeoutException: When generation exceeds the timeout.
            AiException: When no model is loaded or inference fails.
        """
        ...

    def unload(self) -> None:
        """Release the loaded model and free device memory."""
        ...

    def is_loaded(self) -> bool:
        """Return True when a model is currently resident."""
        ...

    def loaded_model(self) -> str | None:
        """Return the model path currently loaded, or None."""
        ...


class TransformersBackend:
    """Default LLM backend backed by transformers + bitsandbytes.

    All heavy imports (``torch``, ``transformers``) happen inside ``load()``
    so the module imports cleanly even when the stack is absent (CI, unit
    tests with a fake backend).
    """

    def __init__(self) -> None:
        """Initialize an empty backend."""
        self._model: Any = None
        self._tokenizer: Any = None
        self._model_path: str | None = None
        self._device: Any = None

    def load(self, model_path: str, quant: str) -> None:
        """Load the model from ``model_path`` with the requested quantization.

        Args:
            model_path: Local model directory.
            quant: Quantization label (gptq-int4 / nf4).

        Raises:
            ModelLoadException: On import, OOM, or load failure.
        """
        torch = _lazy_import_torch()
        transformers = _lazy_import_transformers()
        AutoModelForCausalLM = transformers.AutoModelForCausalLM
        AutoTokenizer = transformers.AutoTokenizer

        quantization_config = self._build_quant_config(
            quant, transformers, torch_module=torch
        )
        LOGGER.info(
            "Loading LLM path=%s quant=%s quant_config=%s",
            model_path,
            quant,
            type(quantization_config).__name__ if quantization_config else "auto",
        )
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                model_path, trust_remote_code=True
            )
            self._model = AutoModelForCausalLM.from_pretrained(
                model_path,
                device_map="auto",
                torch_dtype=torch.float16,
                quantization_config=quantization_config,
                trust_remote_code=True,
            )
        except RuntimeError as error:
            self._reset()
            if _is_oom(error):
                raise ModelLoadException(
                    f"CUDA OOM while loading {model_path}: {error}"
                ) from error
            raise ModelLoadException(
                f"Failed to load model {model_path}: {error}"
            ) from error
        except Exception as error:
            self._reset()
            raise ModelLoadException(
                f"Failed to load model {model_path}: {error}"
            ) from error

        self._model_path = model_path
        try:
            self._device = next(self._model.parameters()).device
        except Exception:  # pragma: no cover - defensive for odd model shells
            self._device = None
        LOGGER.info(
            "LLM loaded path=%s device=%s",
            model_path,
            self._device,
        )

    def generate(
        self,
        prompt: str,
        *,
        max_new_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> GenerateResult:
        """Generate a completion for ``prompt``.

        Args:
            prompt: Input prompt.
            max_new_tokens: Cap on newly generated tokens.
            temperature: Sampling temperature; 0 means greedy.
            timeout_seconds: SLA timeout. Exceeding raises InferenceTimeoutException.

        Returns:
            A GenerateResult with text and timings.

        Raises:
            AiException: When no model is loaded.
            InferenceTimeoutException: On timeout.
        """
        if self._model is None or self._tokenizer is None:
            raise AiException("LLM backend has no loaded model")
        torch = _lazy_import_torch()
        tokenizer = self._tokenizer
        model = self._model

        inputs = tokenizer(prompt, return_tensors="pt")
        input_ids = inputs["input_ids"]
        prompt_tokens = int(input_ids.shape[1])
        if self._device is not None:
            inputs = {k: v.to(self._device) for k, v in inputs.items()}

        do_sample = temperature > 0
        generation_kwargs: dict[str, Any] = {
            "max_new_tokens": max_new_tokens,
            "do_sample": do_sample,
        }
        if do_sample:
            generation_kwargs["temperature"] = temperature

        result_holder: dict[str, Any] = {"result": None, "error": None}

        # M2 review fix (Blocker A): 原实现用 threading.Thread + join(timeout),
        # daemon 线程超时后仍持续占用 GPU 显存且无法中断。改用
        # ThreadPoolExecutor + future.result(timeout=...) + shutdown(wait=False),
        # 超时后主动 unload() 触发 torch.cuda.empty_cache() 释放显存。
        # cancel_event 作为 best-effort cancel token(供未来 model.generate 支持中断时扩展)。
        cancel_event = threading.Event()

        def _run_generation() -> None:
            """Call model.generate and capture output or error."""
            try:
                start = time.perf_counter()
                with torch.no_grad():
                    outputs = model.generate(**inputs, **generation_kwargs)
                first_token_ms = (time.perf_counter() - start) * 1000.0
                result_holder["result"] = (outputs, first_token_ms)
            except Exception as error:  # noqa: BLE001 - surfaced via timeout/result
                result_holder["error"] = error

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="llm-generate"
        )
        future = executor.submit(_run_generation)
        start = time.perf_counter()
        try:
            future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            cancel_event.set()
            future.cancel()
            executor.shutdown(wait=False)
            self.unload()
            raise InferenceTimeoutException(
                f"LLM generate exceeded {timeout_seconds:.1f}s"
            )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        executor.shutdown(wait=False)

        if result_holder["error"] is not None:
            error = result_holder["error"]
            if _is_oom(error):
                self._reset()
                raise ModelLoadException(
                    f"CUDA OOM during generate: {error}"
                ) from error
            raise AiException(f"LLM generate failed: {error}") from error

        outputs, first_token_ms = result_holder["result"]
        completion_ids = outputs[0][prompt_tokens:]
        completion_tokens = int(completion_ids.shape[0])
        text = tokenizer.decode(completion_ids, skip_special_tokens=True)
        return GenerateResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=elapsed_ms,
            first_token_ms=first_token_ms,
        )

    def unload(self) -> None:
        """Drop model + tokenizer references and free CUDA cache."""
        self._reset()
        try:
            import torch  # type: ignore[import-untyped]
        except ImportError:
            return
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def is_loaded(self) -> bool:
        """Return True when a model is resident."""
        return self._model is not None

    def loaded_model(self) -> str | None:
        """Return the loaded model path, or None."""
        return self._model_path

    def _reset(self) -> None:
        """Clear all resident state."""
        self._model = None
        self._tokenizer = None
        self._model_path = None
        self._device = None

    @staticmethod
    def _build_quant_config(
        quant: str, transformers: Any, torch_module: Any = None
    ) -> Any:
        """Build the quantization config for the requested quant label.

        Args:
            quant: Quantization label.
            transformers: The imported transformers module.
            torch_module: Optional pre-imported torch module. When ``None``,
                torch is imported lazily here; this allows tests to inject a
                stub torch without touching the import system.

        Returns:
            A BitsAndBytesConfig for nf4, or None for gptq-int4 (auto-detected).

        Raises:
            ModelLoadException: When the quant label is unsupported or torch
                is required but unavailable.
        """
        if quant == QUANT_GPTQ_INT4:
            return None
        if quant == QUANT_NF4:
            if torch_module is None:
                try:
                    import torch  # type: ignore[import-untyped]
                except ImportError as error:
                    raise ModelLoadException(
                        "torch is required for nf4 quantization"
                    ) from error
                torch_module = torch
            BitsAndBytesConfig = transformers.BitsAndBytesConfig
            return BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch_module.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
        raise ModelLoadException(f"Unsupported quantization: {quant}")


class LlmLoader:
    """Manage a single resident LLM and route generate() calls.

    The loader enforces the spec's "one model at a time" VRAM budget by
    unloading the previous model before loading a new one. Callers pass a
    logical ``model_key`` (e.g. ``"7b"`` / ``"1.5b"``) so repeated loads of
    the same model are no-ops.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        backend: LlmBackend | None = None,
    ) -> None:
        """Configure the loader.

        Args:
            settings: Application settings (drives default generate kwargs).
            backend: Optional backend override for tests. Defaults to
                ``TransformersBackend`` built lazily on first use.
        """
        self.settings = settings or Settings()
        self._backend = backend
        self._loaded_key: str | None = None

    @property
    def backend(self) -> LlmBackend:
        """Return the backend, building the default TransformersBackend lazily.

        Returns:
            The active LlmBackend instance.
        """
        if self._backend is None:
            self._backend = TransformersBackend()
        return self._backend

    def load(self, model_key: str, model_path: str, quant: str) -> None:
        """Load the model identified by ``model_key``.

        Re-loading the same key is a no-op. Loading a different key first
        unloads the resident model.

        Args:
            model_key: Logical model name (e.g. ``"7b"``).
            model_path: Local model directory.
            quant: Quantization label.

        Raises:
            ModelLoadException: When the backend fails to load.
        """
        if self._loaded_key == model_key and self.backend.is_loaded():
            LOGGER.debug("LLM already loaded key=%s; skipping", model_key)
            return
        if self._loaded_key is not None:
            LOGGER.info("Unloading previous LLM key=%s", self._loaded_key)
            self.backend.unload()
            self._loaded_key = None
        self.backend.load(model_path, quant)
        self._loaded_key = model_key

    def generate(
        self,
        prompt: str,
        *,
        max_new_tokens: int | None = None,
        temperature: float = 0.0,
        timeout_seconds: float | None = None,
    ) -> GenerateResult:
        """Generate a completion for ``prompt`` on the resident model.

        Args:
            prompt: Input prompt.
            max_new_tokens: Override default max tokens.
            temperature: Sampling temperature; 0 means greedy.
            timeout_seconds: Override SLA timeout.

        Returns:
            A GenerateResult from the backend.

        Raises:
            AiException: When no model is loaded.
            InferenceTimeoutException: On timeout.
        """
        if self._loaded_key is None or not self.backend.is_loaded():
            raise AiException("No LLM loaded; call load_llm() first")
        return self.backend.generate(
            prompt,
            max_new_tokens=max_new_tokens or self.settings.model_max_new_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds
            or float(self.settings.model_generate_timeout_seconds),
        )

    def unload(self) -> None:
        """Unload the resident model and clear state."""
        if self._loaded_key is None:
            return
        self.backend.unload()
        self._loaded_key = None

    def is_loaded(self) -> bool:
        """Return True when a model is resident."""
        return self._loaded_key is not None and self.backend.is_loaded()

    def loaded_model(self) -> str | None:
        """Return the logical model key currently loaded, or None."""
        return self._loaded_key


def _lazy_import_torch() -> Any:
    """Import torch lazily so the module imports without the stack.

    Returns:
        The torch module.

    Raises:
        ModelLoadException: When torch is not installed.
    """
    try:
        import torch  # type: ignore[import-untyped]
    except ImportError as error:
        raise ModelLoadException(
            "torch is required for LLM inference; install per spec §1"
        ) from error
    return torch


def _lazy_import_transformers() -> Any:
    """Import transformers lazily.

    Returns:
        The transformers module.

    Raises:
        ModelLoadException: When transformers is not installed.
    """
    try:
        import transformers  # type: ignore[import-untyped]
    except ImportError as error:
        raise ModelLoadException(
            "transformers is required for LLM inference; install per spec §4.5"
        ) from error
    return transformers


def _is_oom(error: BaseException) -> bool:
    """Heuristically detect CUDA OOM errors across torch/transformers versions.

    Args:
        error: Exception raised during load/generate.

    Returns:
        True when the error message indicates CUDA OOM.
    """
    message = str(error).lower()
    return (
        "out of memory" in message
        or "cuda oom" in message
        or "oom" in message
        and "cuda" in message
    )
