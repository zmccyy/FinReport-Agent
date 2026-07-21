"""Redis client wrapper with lazy connection + process-wide singleton.

M2.05 spec §3.9 / §5.4.1: ``fin:lock:model:{modelName}`` distributed lock.
The wrapper keeps ``redis`` lazy-imported so the service stays importable
without a running Redis broker (unit tests inject a fake client).
"""

from __future__ import annotations

from typing import Any, Protocol

from app.core.config import Settings
from app.utils.logger import get_logger

LOGGER = get_logger(__name__)


class RedisLike(Protocol):
    """Minimal Redis surface used by the model_lock + VRAM scheduler."""

    def set(
        self, name: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> Any:
        """SET with optional expiry + NX semantics."""
        ...

    def get(self, name: str) -> Any:
        """GET a key; return None when missing."""
        ...

    def delete(self, name: str) -> Any:
        """DEL a key; return number of removed keys."""
        ...

    def expire(self, name: str, ex: int) -> Any:
        """EXPIRE a key; return True/False."""
        ...

    def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        """EVAL a Lua script with ``numkeys`` keys + ``args``."""
        ...

    def ping(self) -> Any:
        """PING the server; raises on failure."""
        ...


class RedisClient:
    """Thin wrapper around ``redis.Redis`` with lazy connection.

    The real ``redis`` module is imported on first ``connect()`` so unit
    tests can construct the wrapper without the dependency and inject a
    fake via ``reset_redis_client``.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Configure the client.

        Args:
            settings: Application settings (drives URL + socket timeouts).
        """
        self.settings = settings or Settings()
        self._client: RedisLike | None = None

    @property
    def client(self) -> RedisLike:
        """Return the underlying Redis client, connecting lazily.

        Returns:
            A connected Redis-like client.

        Raises:
            RuntimeError: When the client was never connected.
        """
        if self._client is None:
            self.connect()
        assert self._client is not None  # for type checkers
        return self._client

    def connect(self) -> None:
        """Build the underlying ``redis.Redis`` from ``settings.redis_url``.

        Raises:
            ImportError: When ``redis`` is not installed.
        """
        if self._client is not None:
            return
        try:
            import redis  # type: ignore[import-untyped]
        except ImportError as error:  # pragma: no cover - dev envs install redis
            raise ImportError(
                "redis is required for ModelHub locks; install per spec §1"
            ) from error
        LOGGER.info(
            "[RedisClient] connecting url=%s", self._mask_url(self.settings.redis_url)
        )
        self._client = redis.Redis.from_url(
            self.settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )

    def close(self) -> None:
        """Drop the underlying connection."""
        if self._client is None:
            return
        try:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
        finally:
            self._client = None

    def ping(self) -> bool:
        """Ping the Redis server.

        Returns:
            True when the server responds; False on connection failure.
        """
        try:
            self.client.ping()
            return True
        except Exception as error:  # noqa: BLE001 - surfaced as False
            LOGGER.warning("[RedisClient] ping failed: %s", error)
            return False

    @staticmethod
    def _mask_url(url: str) -> str:
        """Hide credentials in ``redis://:secret@host:port/db`` URLs.

        Args:
            url: Raw Redis URL.

        Returns:
            A URL with any password component replaced by ``***``.
        """
        try:
            from urllib.parse import urlparse, urlunparse

            parsed = urlparse(url)
            if parsed.password:
                netloc = f"{parsed.username or ''}:***@{parsed.hostname or ''}"
                if parsed.port:
                    netloc = f"{netloc}:{parsed.port}"
                return urlunparse(parsed._replace(netloc=netloc))
            return url
        except Exception:  # pragma: no cover - defensive
            return url


_DEFAULT_CLIENT: RedisClient | None = None


def get_redis_client() -> RedisClient:
    """Return the process-wide Redis client singleton.

    Returns:
        A shared ``RedisClient``; built lazily on first call.
    """
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = RedisClient()
    return _DEFAULT_CLIENT


def reset_redis_client(client: RedisClient | None = None) -> None:
    """Reset the singleton (test helper).

    Args:
        client: Optional override; pass ``None`` to clear.
    """
    global _DEFAULT_CLIENT
    _DEFAULT_CLIENT = client
