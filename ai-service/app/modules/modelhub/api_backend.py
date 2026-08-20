"""M4.02 DeepSeekBackend: LLM inference via the DeepSeek API.

2026-08-16 架构变更（决策记录 2026-08-16-m4-pivot-deepseek-api-rag）：本地训练
取消，LLM 推理统一走 DeepSeek 官方 API（deepseek-chat，OpenAI 兼容协议）。

设计要点：
- 实现 ``LlmBackend`` Protocol（同步 ``httpx.Client``；业务层已用
  ``asyncio.to_thread`` 包装同步调用，因此这里保持同步实现）。
- 重试策略（spec §8.1）：429/5xx/网络错误指数退避（1s, 2s）重试 2 次；
  4xx 不重试直接抛 ``AiException``；超时抛 ``InferenceTimeoutException``。
- json_mode：``response_format={"type": "json_object"}``，用于抽取/复核等
  强约束 JSON 输出场景（调用方 prompt 必须包含 "json" 字样，DeepSeek 协议要求）。
- API Key 仅从环境变量 ``LLM_API_KEY``（经 Settings）注入，不入库不入 git。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.config import Settings
from app.core.exceptions import (
    AiException,
    InferenceTimeoutException,
    ModelLoadException,
)
from app.modules.modelhub.llm_loader import GenerateResult
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)

# API 路由的 quant 标签（M4.08 后唯一后端；本地 gptq-int4/nf4 已随 GPU 栈移除）。
QUANT_API = "api"

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class DeepSeekBackend:
    """``LlmBackend`` implementation backed by the DeepSeek chat API.

    ``load()`` is a cheap idempotent operation (validate key + prepare the
    HTTP client) — there is no resident model, unlike the local
    TransformersBackend it replaced (removed in M4.08).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Configure the backend.

        Args:
            settings: Application settings (API base URL / key / retry knobs).
            transport: Optional httpx transport override for unit tests
                (``httpx.MockTransport``); production leaves it as None.
        """
        self.settings = settings or Settings()
        self._transport = transport
        self._client: httpx.Client | None = None

    def load(self, model_key: str, quant: str) -> None:
        """Prepare the API client (idempotent, no resident model).

        Args:
            model_key: Unused for API backends (kept for Protocol compat).
            quant: Quant label; expected ``"api"`` for documentation only.

        Raises:
            ModelLoadException: When the API key is not configured.
        """
        del model_key, quant
        if not self.settings.llm_api_key:
            raise ModelLoadException(
                "LLM_API_KEY is not configured; set it in deploy/.env (see AGENTS.md §8.1)"
            )
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.settings.llm_api_base_url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=float(self.settings.model_generate_timeout_seconds),
                transport=self._transport,
            )
        LOGGER.info(
            "[DeepSeekBackend.load] base_url=%s model=%s",
            self.settings.llm_api_base_url,
            self.settings.llm_api_model,
        )

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
        """Call ``POST /chat/completions`` and return the decoded text.

        Args:
            prompt: User prompt text.
            max_new_tokens: Mapped to the API ``max_tokens`` field.
            temperature: Sampling temperature (0 = deterministic-ish).
            timeout_seconds: Request timeout (overrides the client default).
            system_prompt: Optional system message prepended to the payload.
            json_mode: When True, request ``response_format=json_object``.

        Returns:
            A GenerateResult with usage stats from the API response.

        Raises:
            InferenceTimeoutException: On request timeout (no retry).
            AiException: On 4xx responses, or retryable errors exhausted.
        """
        if self._client is None:
            self.load("", QUANT_API)
        if self._client is None:
            # 防御性检查：load() 成功路径必置 _client；显式抛错避免
            # python -O 下 assert 被剥离后出现裸 AttributeError。
            raise AiException("DeepSeek API client failed to initialize")

        payload = self._build_payload(
            prompt,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
            json_mode=json_mode,
        )

        max_attempts = max(0, self.settings.llm_api_max_retries) + 1
        last_error: Exception | None = None
        start = time.perf_counter()

        for attempt in range(1, max_attempts + 1):
            try:
                response = self._client.post(
                    "/chat/completions",
                    json=payload,
                    timeout=timeout_seconds,
                )
                response.raise_for_status()
                latency_ms = (time.perf_counter() - start) * 1000.0
                return self._parse_response(response, latency_ms)
            except httpx.TimeoutException as error:
                raise InferenceTimeoutException(
                    f"DeepSeek API timed out after {timeout_seconds:.1f}s"
                ) from error
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                if status_code not in _RETRYABLE_STATUS_CODES:
                    raise AiException(
                        f"DeepSeek API returned {status_code}: {error.response.text[:200]}"
                    ) from error
                last_error = error
                LOGGER.warning(
                    "[DeepSeekBackend] retryable status=%s attempt=%d/%d",
                    status_code,
                    attempt,
                    max_attempts,
                )
            except httpx.TransportError as error:
                last_error = error
                LOGGER.warning(
                    "[DeepSeekBackend] network error=%s attempt=%d/%d",
                    type(error).__name__,
                    attempt,
                    max_attempts,
                )

            if attempt < max_attempts:
                delay = self.settings.llm_api_retry_base_delay_seconds * (2 ** (attempt - 1))
                LOGGER.info("[DeepSeekBackend] retrying in %.1fs", delay)
                time.sleep(delay)

        raise AiException(f"DeepSeek API failed after {max_attempts} attempts: {last_error}")

    def unload(self) -> None:
        """Close the HTTP client (no device memory to free)."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def is_loaded(self) -> bool:
        """Return True when the HTTP client is ready."""
        return self._client is not None

    def loaded_model(self) -> str | None:
        """Return the configured API model name, or None when not loaded."""
        return self.settings.llm_api_model if self._client is not None else None

    def _build_payload(
        self,
        prompt: str,
        *,
        max_new_tokens: int,
        temperature: float,
        system_prompt: str | None,
        json_mode: bool,
    ) -> dict[str, Any]:
        """Assemble the OpenAI-compatible request body.

        Args:
            prompt: User prompt text.
            max_new_tokens: Cap on generated tokens.
            temperature: Sampling temperature.
            system_prompt: Optional system message.
            json_mode: Whether to request JSON-constrained output.

        Returns:
            The JSON-serializable request payload.
        """
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.settings.llm_api_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    @staticmethod
    def _parse_response(response: httpx.Response, latency_ms: float) -> GenerateResult:
        """Extract text + usage from a 2xx chat completion response.

        Args:
            response: The successful httpx response.
            latency_ms: Wall-clock latency measured by the caller.

        Returns:
            A GenerateResult.

        Raises:
            AiException: When the response body is malformed.
        """
        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise AiException(
                f"DeepSeek API returned malformed body: {response.text[:200]}"
            ) from error
        if not content and choice.get("finish_reason") == "length":
            # reasoning 模型（deepseek-v4-flash 等）会把 max_tokens 预算
            # 先花在 reasoning_content 上，content 为空即预算耗尽截断
            # （M4.10 实测 1024 上限时三表抽取全部命中此路径）。
            raise AiException(
                "DeepSeek API output truncated: content empty with "
                "finish_reason=length (reasoning consumed the token budget; "
                "raise model_max_new_tokens)"
            )
        return GenerateResult(
            text=content,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
            first_token_ms=0.0,
        )


__all__ = ["DeepSeekBackend", "QUANT_API"]
