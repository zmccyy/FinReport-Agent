"""M2.04/M4.08 LlmLoader: manage the resident LLM backend and expose generate().

2026-08-16 起本地训练取消（decision record），M4.08 已移除
``TransformersBackend``（torch/transformers 本地推理栈）。当前唯一生产
后端是 ``DeepSeekBackend``（OpenAI 兼容 API，``api_backend.py``）；
``LlmBackend`` 协议保留，便于单测注入 fake 与未来接入其他 API 后端。

The loader keeps a single LLM "resident" at a time (logical model key,
e.g. ``"deepseek-chat"``). Loading a different key first unloads the
previous one — API 后端下 unload 只是关闭 HTTP 客户端与清理状态，
语义保留以兼容 ModelHub 状态查询（``status()`` / ``is_loaded()``）。

Failure modes mapped to ``ModelLoadException`` / ``AiException`` /
``InferenceTimeoutException``:
- backend 未显式注入（生产代码必须传 DeepSeekBackend）
- API Key 缺失 / 4xx 错误（DeepSeekBackend 内部处理）
- 推理超时（InferenceTimeoutException，API 后端映射 HTTP 超时）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.config import Settings
from app.core.exceptions import AiException, ModelLoadException
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)


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
    """Pluggable inference backend contract (M4.08: API-only)."""

    def load(self, model_key: str, quant: str) -> None:
        """Initialize the backend for ``model_key``.

        Args:
            model_key: Logical model name (e.g. ``deepseek-chat``).
            quant: Quantization/routing label (``api``).

        Raises:
            ModelLoadException: When the backend cannot initialize.
        """
        ...

    def generate(
        self,
        prompt: str,
        *,
        max_new_tokens: int,
        temperature: float,
        timeout_seconds: float,
        system_prompt: str | None = None,
        json_mode: bool = False,
    ) -> GenerateResult:
        """Run a single generate call.

        Args:
            prompt: Input prompt text.
            max_new_tokens: Maximum tokens to generate.
            temperature: Sampling temperature (0 = greedy).
            timeout_seconds: Inference SLA timeout.
            system_prompt: Optional system message (API backends).
            json_mode: Request JSON-constrained output (API backends).

        Returns:
            A GenerateResult carrying the decoded text and timings.

        Raises:
            InferenceTimeoutException: When generation exceeds the timeout.
            AiException: When no model is loaded or inference fails.
        """
        ...

    def unload(self) -> None:
        """Release backend resources (HTTP client / cached state)."""
        ...

    def is_loaded(self) -> bool:
        """Return True when the backend is initialized."""
        ...

    def loaded_model(self) -> str | None:
        """Return the logical model key currently loaded, or None."""
        ...


class LlmLoader:
    """Manage a single resident LLM and route generate() calls.

    Re-loading the same key is a no-op. Loading a different key first
    unloads the resident model. Callers pass a logical ``model_key``
    (e.g. ``"deepseek-chat"``).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        backend: LlmBackend | None = None,
    ) -> None:
        """Configure the loader.

        Args:
            settings: Application settings (drives default generate kwargs).
            backend: Backend instance. M4.08 起必须显式传入
                （生产代码传 ``DeepSeekBackend``；单测传 fake）；
                缺省时首次使用抛 ``ModelLoadException``（fail loud，
                避免静默回退到已删除的本地栈）。
        """
        self.settings = settings or Settings()
        self._backend = backend
        self._loaded_key: str | None = None

    @property
    def backend(self) -> LlmBackend:
        """Return the backend, failing loudly when none was injected.

        Returns:
            The active LlmBackend instance.

        Raises:
            ModelLoadException: When no backend was provided (M4.08 移除
                了默认 TransformersBackend，生产代码必须传 API 后端)。
        """
        if self._backend is None:
            raise ModelLoadException(
                "LlmLoader requires an explicit backend "
                "(e.g. DeepSeekBackend); the local TransformersBackend "
                "was removed in M4.08"
            )
        return self._backend

    def load(self, model_key: str, model_path: str, quant: str) -> None:
        """Load the model identified by ``model_key``.

        Re-loading the same key is a no-op. Loading a different key first
        unloads the resident model.

        Args:
            model_key: Logical model name (e.g. ``deepseek-chat``).
            model_path: 本地模型目录（API 路由传空串，M4.08 后仅为
                协议兼容保留参数）。
            quant: Quantization/routing label (``api``).

        Raises:
            ModelLoadException: When the backend fails to load.
        """
        del model_path  # API 后端无本地产物（协议兼容保留）
        if self._loaded_key == model_key and self.backend.is_loaded():
            LOGGER.debug("LLM already loaded key=%s; skipping", model_key)
            return
        if self._loaded_key is not None:
            LOGGER.info("Unloading previous LLM key=%s", self._loaded_key)
            self.backend.unload()
            self._loaded_key = None
        self.backend.load(model_key, quant)
        self._loaded_key = model_key

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
        """Generate a completion for ``prompt`` on the resident model.

        Args:
            prompt: Input prompt.
            max_new_tokens: Override default max tokens.
            temperature: Override default temperature.
            timeout_seconds: Override SLA timeout.
            system_prompt: Optional system message (API backends).
            json_mode: Request JSON-constrained output (API backends).

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
            timeout_seconds=timeout_seconds or float(self.settings.model_generate_timeout_seconds),
            system_prompt=system_prompt,
            json_mode=json_mode,
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
