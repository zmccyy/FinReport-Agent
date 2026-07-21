"""M2.05 ModelLock + VramScheduler tests.

Uses a hand-rolled ``_FakeRedis`` to avoid pulling in ``fakeredis``
(AGENTS.md §9.2 forbids new deps not in spec §1). The fake implements
the ``RedisLike`` Protocol surface: ``set`` / ``get`` / ``delete`` /
``expire`` / ``eval`` / ``ping`` plus minimal Lua interpretation for
the release + refresh scripts.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import (
    LockAcquisitionException,
    ModelLockBusyException,
    ModelLoadException,
)
from app.core.redis_client import RedisClient, get_redis_client, reset_redis_client
from app.modules.modelhub import (
    MODEL_LOCK_KEY_PREFIX,
    ModelHub,
    ModelLock,
    Scene,
    VramScheduler,
)
from app.modules.modelhub.llm_loader import LlmLoader
from app.modules.modelhub.vram_scheduler import scene_for_quant

# ---------------------------------------------------------------------------
# _FakeRedis — minimal Redis-like backend for unit tests
# ---------------------------------------------------------------------------


class _FakeRedis:
    """In-memory Redis stub honouring SET NX EX + Lua release/refresh."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._ttl: dict[str, float] = {}
        self._now = time.monotonic()
        self.set_calls: list[dict[str, Any]] = []
        self.eval_calls: list[dict[str, Any]] = []
        self.ping_calls: int = 0
        self.fail_ping: bool = False
        self.fail_set: Exception | None = None

    def _expired(self, key: str) -> bool:
        ttl = self._ttl.get(key)
        if ttl is None:
            return False
        return time.monotonic() > ttl

    def set(
        self, name: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> bool:
        """SET with optional EX + NX semantics."""
        if self.fail_set is not None:
            raise self.fail_set
        self.set_calls.append({"name": name, "value": value, "ex": ex, "nx": nx})
        if self._expired(name):
            self._store.pop(name, None)
            self._ttl.pop(name, None)
        if nx and name in self._store and not self._expired(name):
            return False
        self._store[name] = value
        if ex is not None:
            self._ttl[name] = time.monotonic() + ex
        else:
            self._ttl.pop(name, None)
        return True

    def get(self, name: str) -> str | None:
        """GET a key; return None when missing or expired."""
        if self._expired(name):
            self._store.pop(name, None)
            self._ttl.pop(name, None)
            return None
        return self._store.get(name)

    def delete(self, name: str) -> int:
        """DEL a key; return number of removed keys."""
        removed = 1 if name in self._store else 0
        self._store.pop(name, None)
        self._ttl.pop(name, None)
        return removed

    def expire(self, name: str, ex: int) -> bool:
        """EXPIRE a key; return True/False."""
        if name not in self._store or self._expired(name):
            return False
        self._ttl[name] = time.monotonic() + ex
        return True

    def eval(self, script: str, numkeys: int, *args: Any) -> int:
        """Interpret the two Lua scripts we use (release + refresh)."""
        del numkeys
        self.eval_calls.append({"script": script, "args": args})
        # Scripts: KEYS[1]=args[0], ARGV[1]=args[1], ARGV[2]=args[2] (refresh)
        key = args[0]
        expected = args[1]
        current = self.get(key)
        if current != expected:
            return 0
        if "expire" in script:
            ttl = int(args[2])
            self._ttl[key] = time.monotonic() + ttl
            return 1
        if "del" in script:
            self.delete(key)
            return 1
        return 0

    def ping(self) -> bool:
        """PING stub."""
        self.ping_calls += 1
        if self.fail_ping:
            raise RuntimeError("redis down")
        return True

    def advance_time(self, seconds: float) -> None:
        """Helper for tests that need to expire TTLs without sleeping."""
        # Push all TTLs back in time.
        for k in list(self._ttl.keys()):
            self._ttl[k] -= seconds


# ---------------------------------------------------------------------------
# Reused fake backend (mirrors test_m2_modelhub's _FakeBackend minimal surface)
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Records load/unload calls without touching torch/transformers."""

    def __init__(self, *, load_error: Exception | None = None) -> None:
        self.load_calls: list[tuple[str, str]] = []
        self.unload_calls: int = 0
        self._loaded_key: str | None = None
        self._load_error = load_error

    def load(self, model_path: str, quant: str) -> None:
        self.load_calls.append((model_path, quant))
        if self._load_error is not None:
            raise self._load_error
        self._loaded_key = model_path

    def generate(
        self,
        prompt: str,
        *,
        max_new_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> Any:
        raise NotImplementedError

    def unload(self) -> None:
        self.unload_calls += 1
        self._loaded_key = None

    def is_loaded(self) -> bool:
        return self._loaded_key is not None

    def loaded_model(self) -> str | None:
        return self._loaded_key


def _hub(backend: _FakeBackend | None = None) -> ModelHub:
    return ModelHub(llm_loader=LlmLoader(backend=backend or _FakeBackend()))


# ---------------------------------------------------------------------------
# RedisClient tests
# ---------------------------------------------------------------------------


def test_redis_client_lazy_connect_returns_ping() -> None:
    """RedisClient.connect builds the client lazily and ping succeeds."""
    rc = RedisClient(Settings(redis_url="redis://localhost:6379/0"))
    # Inject fake client to avoid pulling in real redis.
    fake = _FakeRedis()
    rc._client = fake  # type: ignore[attr-defined]
    assert rc.ping() is True
    assert fake.ping_calls == 1


def test_redis_client_ping_swallows_errors() -> None:
    """Ping failures return False instead of propagating."""
    rc = RedisClient()
    fake = _FakeRedis()
    fake.fail_ping = True
    rc._client = fake  # type: ignore[attr-defined]
    assert rc.ping() is False


def test_redis_client_close_drops_underlying_client() -> None:
    """close() clears the underlying client so next access reconnects."""
    rc = RedisClient()
    rc._client = _FakeRedis()  # type: ignore[attr-defined]
    rc.close()
    assert rc._client is None


def test_get_redis_client_singleton() -> None:
    """get_redis_client returns the same instance across calls."""
    reset_redis_client(None)
    try:
        a = get_redis_client()
        b = get_redis_client()
        assert a is b
    finally:
        reset_redis_client(None)


def test_reset_redis_client_clears_singleton() -> None:
    """reset_redis_client(None) clears the cached singleton."""
    reset_redis_client(None)
    a = get_redis_client()
    reset_redis_client(None)
    b = get_redis_client()
    try:
        assert a is not b
    finally:
        reset_redis_client(None)


def test_mask_url_hides_password() -> None:
    """RedisClient._mask_url replaces password with ***."""
    masked = RedisClient._mask_url("redis://:secret@host:6379/0")
    assert "secret" not in masked
    assert "***" in masked
    assert "host:6379" in masked


# ---------------------------------------------------------------------------
# ModelLock tests
# ---------------------------------------------------------------------------


def test_lock_acquire_returns_true_and_sets_key() -> None:
    """First acquire succeeds and stores the worker_id at the lock key."""
    fake = _FakeRedis()
    lock = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)
    assert lock.acquire() is True
    assert lock.is_acquired is True
    assert fake.get(f"{MODEL_LOCK_KEY_PREFIX}7b") == "w1"
    assert len(fake.set_calls) == 1
    assert fake.set_calls[0]["nx"] is True
    assert fake.set_calls[0]["ex"] == 60


def test_lock_acquire_returns_false_when_held() -> None:
    """Second acquire from a different worker fails (NX semantics)."""
    fake = _FakeRedis()
    lock1 = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)
    lock2 = ModelLock("7b", fake, worker_id="w2", ttl_seconds=60)
    assert lock1.acquire() is True
    assert lock2.acquire() is False
    assert lock2.is_acquired is False


def test_lock_acquire_or_raise_raises_busy_when_held() -> None:
    """acquire_or_raise raises ModelLockBusyException on contention."""
    fake = _FakeRedis()
    ModelLock("7b", fake, worker_id="w1", ttl_seconds=60).acquire()
    lock2 = ModelLock("7b", fake, worker_id="w2", ttl_seconds=60)
    with pytest.raises(ModelLockBusyException, match="held by another worker"):
        lock2.acquire_or_raise()


def test_lock_acquire_maps_redis_error_to_lock_exception() -> None:
    """Redis errors during SET surface as LockAcquisitionException."""
    fake = _FakeRedis()
    fake.fail_set = RuntimeError("redis connection lost")
    lock = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)
    with pytest.raises(LockAcquisitionException, match="Failed to acquire"):
        lock.acquire()


def test_lock_release_only_deletes_when_owner_matches() -> None:
    """release() deletes the key when worker_id matches; no-op otherwise."""
    fake = _FakeRedis()
    lock = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)
    lock.acquire()

    # Different worker steals the key (simulating TTL expiry + reacquire).
    fake._store[f"{MODEL_LOCK_KEY_PREFIX}7b"] = "w2"
    deleted = lock.release()

    assert deleted is False
    assert lock.is_acquired is False
    # Key still present (owned by w2)
    assert fake.get(f"{MODEL_LOCK_KEY_PREFIX}7b") == "w2"


def test_lock_release_deletes_when_owner_matches() -> None:
    """release() returns True and deletes the key when owner matches."""
    fake = _FakeRedis()
    lock = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)
    lock.acquire()

    deleted = lock.release()

    assert deleted is True
    assert fake.get(f"{MODEL_LOCK_KEY_PREFIX}7b") is None


def test_lock_release_no_op_when_not_acquired() -> None:
    """release() before acquire() is a no-op returning False."""
    fake = _FakeRedis()
    lock = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)
    assert lock.release() is False


def test_lock_release_maps_redis_error_to_lock_exception() -> None:
    """Redis errors during EVAL surface as LockAcquisitionException."""
    fake = _FakeRedis()
    lock = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)
    lock.acquire()

    original_eval = fake.eval

    def raising_eval(script: str, numkeys: int, *args: Any) -> int:
        raise RuntimeError("eval boom")

    fake.eval = raising_eval  # type: ignore[assignment]
    try:
        with pytest.raises(LockAcquisitionException, match="Failed to release"):
            lock.release()
    finally:
        fake.eval = original_eval  # type: ignore[assignment]


def test_lock_refresh_extends_ttl_when_owner_matches() -> None:
    """refresh() extends the TTL only for the owning worker."""
    fake = _FakeRedis()
    lock = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)
    lock.acquire()

    original_ttl = fake._ttl[f"{MODEL_LOCK_KEY_PREFIX}7b"]
    # Sleep past Windows' ~15ms monotonic granularity so the new TTL lands
    # strictly after the original.
    time.sleep(0.05)
    assert lock.refresh() is True
    new_ttl = fake._ttl[f"{MODEL_LOCK_KEY_PREFIX}7b"]
    assert new_ttl > original_ttl


def test_lock_refresh_returns_false_when_not_owner() -> None:
    """refresh() returns False when the lock is held by another worker."""
    fake = _FakeRedis()
    lock = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)
    lock.acquire()
    fake._store[f"{MODEL_LOCK_KEY_PREFIX}7b"] = "w2"

    assert lock.refresh() is False


def test_lock_refresh_returns_false_when_not_acquired() -> None:
    """refresh() before acquire() is a no-op returning False."""
    fake = _FakeRedis()
    lock = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)
    assert lock.refresh() is False


def test_lock_context_manager_acquires_and_releases() -> None:
    """``with lock:`` acquires on entry and releases on exit."""
    fake = _FakeRedis()
    lock = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)

    with lock:
        assert lock.is_acquired is True
        assert fake.get(f"{MODEL_LOCK_KEY_PREFIX}7b") == "w1"

    assert lock.is_acquired is False
    assert fake.get(f"{MODEL_LOCK_KEY_PREFIX}7b") is None


def test_lock_context_manager_releases_on_exception() -> None:
    """Lock is released even when the body raises."""
    fake = _FakeRedis()
    lock = ModelLock("7b", fake, worker_id="w1", ttl_seconds=60)

    with pytest.raises(ValueError, match="boom"):
        with lock:
            raise ValueError("boom")

    assert lock.is_acquired is False
    assert fake.get(f"{MODEL_LOCK_KEY_PREFIX}7b") is None


def test_lock_context_manager_raises_busy_when_contended() -> None:
    """Context manager entry raises ModelLockBusyException on contention."""
    fake = _FakeRedis()
    ModelLock("7b", fake, worker_id="w1", ttl_seconds=60).acquire()
    lock2 = ModelLock("7b", fake, worker_id="w2", ttl_seconds=60)

    with pytest.raises(ModelLockBusyException):
        with lock2:
            pass  # pragma: no cover - never reached


def test_lock_key_uses_prefix() -> None:
    """Lock key follows the spec §5.4.1 ``fin:lock:model:{name}`` convention."""
    fake = _FakeRedis()
    lock = ModelLock("1.5b", fake, worker_id="w1", ttl_seconds=60)
    assert lock.key == "fin:lock:model:1.5b"


# ---------------------------------------------------------------------------
# VramScheduler tests
# ---------------------------------------------------------------------------


def test_scheduler_load_for_scene_with_lock_acquires_then_loads() -> None:
    """load_for_scene_with_lock acquires lock + delegates to ModelHub."""
    fake = _FakeRedis()
    backend = _FakeBackend()
    hub = _hub(backend)
    sched = VramScheduler(hub, redis_client=fake)

    lock = sched.load_for_scene_with_lock(Scene.REASON)

    assert lock.is_acquired is True
    assert hub.llm_loader.loaded_model() == "7b"
    assert backend.load_calls[0][1] == "gptq-int4"
    # Lock not auto-released — caller owns it now.
    lock.release()


def test_scheduler_load_for_scene_with_lock_raises_busy_when_lock_held() -> None:
    """Scheduler raises ModelLockBusyException when another worker holds the lock."""
    fake = _FakeRedis()
    # Pre-acquire the 7b lock as if another worker held it.
    ModelLock("7b", fake, worker_id="other", ttl_seconds=60).acquire()

    backend = _FakeBackend()
    hub = _hub(backend)
    sched = VramScheduler(hub, redis_client=fake)

    with pytest.raises(ModelLockBusyException, match="held by another worker"):
        sched.load_for_scene_with_lock(Scene.REASON)

    # Hub did not load anything.
    assert backend.load_calls == []
    assert hub.llm_loader.loaded_model() is None


def test_scheduler_load_releases_lock_when_load_fails() -> None:
    """When ModelHub.load_for_scene fails, the lock is released."""
    fake = _FakeRedis()
    backend = _FakeBackend(load_error=ModelLoadException("missing weights"))
    hub = _hub(backend)
    sched = VramScheduler(hub, redis_client=fake)

    with pytest.raises(ModelLoadException, match="missing weights"):
        sched.load_for_scene_with_lock(Scene.REASON)

    # Lock was released back to the pool.
    assert fake.get(f"{MODEL_LOCK_KEY_PREFIX}7b") is None
    assert hub.llm_loader.loaded_model() is None


def test_scheduler_load_rejects_non_llm_scene() -> None:
    """EMBED / LAYOUT scenes are not loadable through the LLM scheduler."""
    fake = _FakeRedis()
    hub = _hub()
    sched = VramScheduler(hub, redis_client=fake)

    with pytest.raises(ModelLockBusyException, match="does not route"):
        sched.load_for_scene_with_lock(Scene.EMBED)
    with pytest.raises(ModelLockBusyException, match="does not route"):
        sched.load_for_scene_with_lock(Scene.LAYOUT)


def test_scheduler_touch_updates_last_used() -> None:
    """touch() refreshes last_used for a model key."""
    fake = _FakeRedis()
    hub = _hub()
    sched = VramScheduler(hub, redis_client=fake)

    sched.touch("7b")
    sched.touch("1.5b")

    status = sched.status()
    assert "7b" in status["last_used"]
    assert "1.5b" in status["last_used"]


def test_scheduler_evict_idle_unloads_idle_model() -> None:
    """evict_idle unloads a model idle longer than the threshold."""
    fake = _FakeRedis()
    backend = _FakeBackend()
    hub = _hub(backend)
    sched = VramScheduler(
        hub,
        redis_client=fake,
        settings=Settings(vram_idle_threshold_seconds=0),
    )
    sched.load_for_scene_with_lock(Scene.REASON).release()

    # Move last_used into the past beyond threshold=0.
    sched._last_used["7b"] = time.monotonic() - 10

    unloaded = sched.evict_idle()

    assert unloaded == ["7b"]
    assert backend.unload_calls == 1
    assert hub.llm_loader.loaded_model() is None


def test_scheduler_evict_idle_skips_fresh_model() -> None:
    """evict_idle does not unload a model still within the threshold."""
    fake = _FakeRedis()
    backend = _FakeBackend()
    hub = _hub(backend)
    sched = VramScheduler(
        hub,
        redis_client=fake,
        settings=Settings(vram_idle_threshold_seconds=600),
    )
    sched.load_for_scene_with_lock(Scene.REASON).release()

    unloaded = sched.evict_idle()

    assert unloaded == []
    assert backend.unload_calls == 0


def test_scheduler_evict_idle_drops_stale_entries() -> None:
    """Stale last_used entries for non-resident models are dropped silently."""
    fake = _FakeRedis()
    hub = _hub()
    sched = VramScheduler(hub, redis_client=fake)
    sched._last_used["7b"] = time.monotonic() - 9999  # not loaded

    unloaded = sched.evict_idle(idle_threshold_seconds=0)

    assert unloaded == []
    assert "7b" not in sched._last_used


def test_scheduler_status_includes_hub_snapshot() -> None:
    """status() reports worker_id + last_used + the underlying hub state."""
    fake = _FakeRedis()
    backend = _FakeBackend()
    hub = _hub(backend)
    sched = VramScheduler(hub, redis_client=fake)

    status = sched.status()
    assert "worker_id" in status
    assert status["last_used"] == {}
    assert status["hub"]["loaded_llm"] is None


def test_scheduler_uses_redis_client_wrapper() -> None:
    """VramScheduler accepts a RedisClient wrapper and unwraps it."""
    fake = _FakeRedis()
    rc = RedisClient()
    rc._client = fake  # type: ignore[attr-defined]
    hub = _hub()
    sched = VramScheduler(hub, redis_client=rc)

    lock = sched.load_for_scene_with_lock(Scene.REASON)
    assert lock.is_acquired is True
    assert fake.get(f"{MODEL_LOCK_KEY_PREFIX}7b") is not None
    lock.release()


def test_scheduler_loads_same_model_after_lock_release() -> None:
    """After release, the same worker can re-acquire the lock (no re-load)."""
    fake = _FakeRedis()
    backend = _FakeBackend()
    hub = _hub(backend)
    sched = VramScheduler(hub, redis_client=fake)

    lock1 = sched.load_for_scene_with_lock(Scene.REASON)
    lock1.release()

    lock2 = sched.load_for_scene_with_lock(Scene.REASON)
    # LlmLoader's same-key no-op means backend.load only called once.
    assert len(backend.load_calls) == 1
    lock2.release()


def test_scheduler_loads_different_model_unloads_previous() -> None:
    """Loading 1.5b after 7b triggers unload + new load (spec §8.1)."""
    fake = _FakeRedis()
    backend = _FakeBackend()
    hub = _hub(backend)
    sched = VramScheduler(hub, redis_client=fake)

    lock1 = sched.load_for_scene_with_lock(Scene.REASON)
    lock1.release()
    lock2 = sched.load_for_scene_with_lock(Scene.EXTRACT)
    lock2.release()

    assert backend.unload_calls == 1
    assert len(backend.load_calls) == 2


# ---------------------------------------------------------------------------
# scene_for_quant helper tests
# ---------------------------------------------------------------------------


def test_scene_for_quant_gptq_returns_reason() -> None:
    """GPTQ-Int4 quant maps to the REASON scene."""
    assert scene_for_quant("gptq-int4") == Scene.REASON


def test_scene_for_quant_nf4_returns_extract() -> None:
    """NF4 quant maps to the EXTRACT scene."""
    assert scene_for_quant("nf4") == Scene.EXTRACT


def test_scene_for_quant_unknown_raises() -> None:
    """Unknown quant labels raise ValueError."""
    with pytest.raises(ValueError, match="Unknown quant"):
        scene_for_quant("int8")
