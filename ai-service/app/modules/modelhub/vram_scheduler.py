"""M2.05 VRAM scheduler + Redis distributed model_lock (spec §3.9 / §5.4.1).

Two cooperating pieces:

* ``ModelLock`` — a non-reentrant Redis lock keyed ``fin:lock:model:{name}``
  with owner verification (Lua) on release/refresh. ``acquire`` returns
  ``True``/``False``; busy callers raise ``ModelLockBusyException`` via
  ``acquire_or_raise`` so MQ handlers can ``nack(requeue=True)``.
* ``VramScheduler`` — wraps a ``ModelHub`` and tracks per-model
  ``last_used_at``. ``load_for_scene_with_lock`` acquires the model_lock
  before delegating to ``ModelHub.load_for_scene``; ``evict_idle`` unloads
  models idle longer than the configured threshold.

The scheduler does NOT manage lock lifecycle for ``generate()`` — callers
hold the lock for the full inference window via the context manager.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from app.core.config import Settings
from app.core.exceptions import (
    LockAcquisitionException,
    ModelLockBusyException,
)
from app.core.redis_client import RedisLike, RedisClient
from app.modules.modelhub.llm_loader import QUANT_GPTQ_INT4, QUANT_NF4
from app.modules.modelhub.modelhub import LLM_SCENES, ModelHub, Scene
from app.utils.logger import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

LOGGER = get_logger(__name__)

# Redis key conventions (spec §5.4.1).
MODEL_LOCK_KEY_PREFIX = "fin:lock:model:"
MODEL_LOCK_TTL_SECONDS_DEFAULT = 300  # spec §5.4.1: TTL 5min

# Lua script: atomically DEL only when the stored value matches the owner.
# Returns 1 when deleted, 0 otherwise. ``redis.eval`` is invoked with
# ``numkeys=1`` and the key + expected value as args.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# Lua script: atomically EXPIRE only when the stored value matches the owner.
_REFRESH_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""


class ModelLock:
    """Non-reentrant Redis lock for one model (spec §3.9).

    The lock is identified by ``model_name`` (e.g. ``"7b"``) and owned by
    a per-instance ``worker_id`` (UUID4). ``acquire`` uses ``SET NX EX``
    so two workers racing on the same key cannot both succeed. ``release``
    and ``refresh`` verify ownership via Lua to prevent a worker from
    releasing a lock it no longer owns (TTL expiry + re-acquire by peer).
    """

    def __init__(
        self,
        model_name: str,
        client: RedisLike,
        *,
        worker_id: str | None = None,
        ttl_seconds: int = MODEL_LOCK_TTL_SECONDS_DEFAULT,
    ) -> None:
        """Configure the lock.

        Args:
            model_name: Logical model key (e.g. ``"7b"`` / ``"1.5b"``).
            client: Redis-like client (real ``redis.Redis`` or test fake).
            worker_id: Optional owner identifier; auto-generated UUID4.
            ttl_seconds: Lock TTL in seconds (spec §5.4.1: 5min default).
        """
        self.model_name = model_name
        self.client = client
        self.worker_id = worker_id or str(uuid.uuid4())
        self.ttl_seconds = ttl_seconds
        self._key = f"{MODEL_LOCK_KEY_PREFIX}{model_name}"
        self._acquired = False

    @property
    def key(self) -> str:
        """Return the Redis key for this lock."""
        return self._key

    @property
    def is_acquired(self) -> bool:
        """Return whether this instance currently holds the lock."""
        return self._acquired

    def acquire(self) -> bool:
        """Try to acquire the lock once.

        Returns:
            True when acquired; False when held by another worker.

        Raises:
            LockAcquisitionException: When Redis errors during SET.
        """
        try:
            result = self.client.set(
                self._key, self.worker_id, ex=self.ttl_seconds, nx=True
            )
        except (
            Exception
        ) as error:  # noqa: BLE001 - surfaced as LockAcquisitionException
            raise LockAcquisitionException(
                f"Failed to acquire model_lock for {self.model_name}: {error}"
            ) from error
        acquired = bool(result)
        if acquired:
            self._acquired = True
            LOGGER.info(
                "[ModelLock] acquired model=%s worker=%s ttl=%ss",
                self.model_name,
                self.worker_id,
                self.ttl_seconds,
            )
        else:
            LOGGER.debug(
                "[ModelLock] busy model=%s worker=%s", self.model_name, self.worker_id
            )
        return acquired

    def acquire_or_raise(self) -> None:
        """Acquire the lock or raise ``ModelLockBusyException``.

        Raises:
            ModelLockBusyException: When the lock is held by another worker.
            LockAcquisitionException: When Redis errors during SET.
        """
        if not self.acquire():
            raise ModelLockBusyException(
                f"model_lock for {self.model_name} is held by another worker"
            )

    def release(self) -> bool:
        """Release the lock only if this instance still owns it.

        Returns:
            True when the lock was deleted; False otherwise (expired or
            stolen by another worker).
        """
        if not self._acquired:
            return False
        try:
            deleted = self.client.eval(_RELEASE_SCRIPT, 1, self._key, self.worker_id)
        except (
            Exception
        ) as error:  # noqa: BLE001 - surfaced as LockAcquisitionException
            raise LockAcquisitionException(
                f"Failed to release model_lock for {self.model_name}: {error}"
            ) from error
        self._acquired = False
        LOGGER.info(
            "[ModelLock] released model=%s worker=%s deleted=%s",
            self.model_name,
            self.worker_id,
            bool(deleted),
        )
        return bool(deleted)

    def refresh(self) -> bool:
        """Extend the TTL only if this instance still owns the lock.

        Returns:
            True when the TTL was extended; False otherwise.
        """
        if not self._acquired:
            return False
        try:
            refreshed = self.client.eval(
                _REFRESH_SCRIPT, 1, self._key, self.worker_id, str(self.ttl_seconds)
            )
        except (
            Exception
        ) as error:  # noqa: BLE001 - surfaced as LockAcquisitionException
            raise LockAcquisitionException(
                f"Failed to refresh model_lock for {self.model_name}: {error}"
            ) from error
        return bool(refreshed)

    def __enter__(self) -> "ModelLock":
        """Context manager entry: acquire or raise."""
        self.acquire_or_raise()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """Context manager exit: always release."""
        self.release()


class VramScheduler:
    """Wrap ModelHub with model_lock acquisition + LRU idle eviction.

    The scheduler records ``last_used_at`` for each model after a
    successful ``load_for_scene_with_lock`` and exposes ``evict_idle``
    so a background tick can release idle models (spec §3.9 — avoid VRAM
    jitter between EXTRACT/REASON chains).
    """

    def __init__(
        self,
        hub: ModelHub,
        redis_client: RedisClient | RedisLike | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Configure the scheduler.

        Args:
            hub: ModelHub instance to wrap.
            redis_client: Optional Redis client (real ``RedisClient`` or
                a raw Redis-like client for tests). When ``None``, the
                process-wide ``get_redis_client()`` singleton is used.
            settings: Application settings (drives TTL + idle threshold).
        """
        self.hub = hub
        self.settings = settings or hub.settings
        if redis_client is None:
            from app.core.redis_client import get_redis_client

            redis_client = get_redis_client().client
        elif isinstance(redis_client, RedisClient):
            redis_client = redis_client.client
        self._client: RedisLike = redis_client  # type: ignore[assignment]
        self._last_used: dict[str, float] = {}
        self._worker_id = str(uuid.uuid4())

    def load_for_scene_with_lock(self, scene: Scene) -> ModelLock:
        """Acquire the model_lock for ``scene`` then load the model.

        Args:
            scene: Inference scene (must be in ``LLM_SCENES``).

        Returns:
            The acquired ``ModelLock``; caller MUST ``release()`` or use
            as a context manager after inference completes.

        Raises:
            AiException: When the scene does not route through LlmLoader.
            ModelLockBusyException: When the lock is held by another worker.
            ModelLoadException: When the backend fails to load.
        """
        if scene not in LLM_SCENES:
            raise ModelLockBusyException(
                f"Scene {scene.value} does not route through LlmLoader"
            )
        model_key, _ = self.hub.route(scene)
        lock = ModelLock(
            model_key,
            self._client,
            worker_id=self._worker_id,
            ttl_seconds=self.settings.model_lock_ttl_seconds,
        )
        lock.acquire_or_raise()
        try:
            self.hub.load_for_scene(scene)
        except Exception:
            lock.release()
            raise
        self._last_used[model_key] = time.monotonic()
        LOGGER.info(
            "[VramScheduler] loaded scene=%s model=%s",
            scene.value,
            model_key,
        )
        return lock

    def touch(self, model_key: str) -> None:
        """Mark ``model_key`` as recently used.

        Args:
            model_key: Logical model key (e.g. ``"7b"``).
        """
        self._last_used[model_key] = time.monotonic()

    def evict_idle(self, idle_threshold_seconds: int | None = None) -> list[str]:
        """Unload models idle longer than the threshold.

        Args:
            idle_threshold_seconds: Override ``Settings.vram_idle_threshold_seconds``.

        Returns:
            A list of model keys that were unloaded.
        """
        threshold = (
            idle_threshold_seconds
            if idle_threshold_seconds is not None
            else self.settings.vram_idle_threshold_seconds
        )
        now = time.monotonic()
        unloaded: list[str] = []
        loaded = self.hub.llm_loader.loaded_model()
        for model_key, last_used in list(self._last_used.items()):
            if loaded == model_key:
                # Skip the currently resident model — only evict stale entries
                # when another model has since loaded.
                if now - last_used < threshold:
                    continue
                LOGGER.info(
                    "[VramScheduler] evicting idle model=%s idle_seconds=%.1f",
                    model_key,
                    now - last_used,
                )
                self.hub.unload()
                unloaded.append(model_key)
                self._last_used.pop(model_key, None)
            else:
                # Stale entry for a model no longer resident — drop it.
                self._last_used.pop(model_key, None)
        return unloaded

    def status(self) -> dict[str, object]:
        """Return a JSON-serializable snapshot of the scheduler state.

        Returns:
            A dict with worker_id, last_used per model, and the underlying
            ModelHub status.
        """
        now = time.monotonic()
        return {
            "worker_id": self._worker_id,
            "last_used": {k: round(now - v, 1) for k, v in self._last_used.items()},
            "hub": self.hub.status(),
        }


def scene_for_quant(quant: str) -> Scene:
    """Return the canonical scene for a quant label (test helper).

    Args:
        quant: Quantization label (``gptq-int4`` / ``nf4``).

    Returns:
        ``Scene.REASON`` for GPTQ-Int4, ``Scene.EXTRACT`` for NF4.

    Raises:
        ValueError: When the quant label is unknown.
    """
    if quant == QUANT_GPTQ_INT4:
        return Scene.REASON
    if quant == QUANT_NF4:
        return Scene.EXTRACT
    raise ValueError(f"Unknown quant: {quant}")


__all__ = [
    "MODEL_LOCK_KEY_PREFIX",
    "MODEL_LOCK_TTL_SECONDS_DEFAULT",
    "ModelLock",
    "VramScheduler",
    "scene_for_quant",
]
