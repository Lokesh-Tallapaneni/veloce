"""`InMemorySessionStore` — periodic eviction of expired entries.

Without a sweep mechanism, sessions that are written and never re-read
sit in `_entries` until the process exits (slow memory leak under
create-and-forget traffic). The store offers both:

* a probabilistic sweep on every `write`/`replace`, gated by entry
  count + a random check (cheap, transparent), and
* an explicit `sweep_expired()` that callers can wire into a background
  task for deterministic eviction.
"""

from __future__ import annotations

import asyncio
import time

from veloce import InMemorySessionStore
from veloce import sessions as sessions_module


def _run(coro):  # tiny helper so the tests stay sync-friendly
    return asyncio.new_event_loop().run_until_complete(coro)


def test_sweep_expired_returns_count_and_removes_expired() -> None:
    store = InMemorySessionStore()
    # Three already-expired entries written directly into the backing
    # dict — bypass `write` so the probabilistic sweep doesn't
    # interfere with what we're measuring.
    past = time.time() - 1
    store._entries["a"] = ({"v": 1}, past)
    store._entries["b"] = ({"v": 2}, past)
    store._entries["c"] = ({"v": 3}, past)
    # One live entry that must survive.
    store._entries["live"] = ({"v": 4}, time.time() + 3600)

    removed = store.sweep_expired()

    assert removed == 3
    assert "live" in store._entries
    assert set(store._entries) == {"live"}


def test_sweep_expired_does_not_touch_live_entries() -> None:
    store = InMemorySessionStore()
    future = time.time() + 3600
    store._entries["x"] = ({"v": 1}, future)
    store._entries["y"] = ({"v": 2}, future)

    assert store.sweep_expired() == 0
    assert set(store._entries) == {"x", "y"}


def test_probabilistic_sweep_fires_on_write_above_threshold(monkeypatch) -> None:
    store = InMemorySessionStore()
    # Pre-populate above the threshold with already-expired entries.
    past = time.time() - 1
    n = sessions_module._SWEEP_THRESHOLD + 5
    for i in range(n):
        store._entries[f"expired-{i}"] = ({"i": i}, past)

    # Force the dice: `random.random()` returns 0.0, which is strictly
    # less than `_SWEEP_PROBABILITY`, so `_maybe_sweep` must scan.
    monkeypatch.setattr(sessions_module.random, "random", lambda: 0.0)

    # Trigger one `write` — the new entry is live, every other is
    # expired and must be evicted by the sweep `write` invokes.
    _run(store.write("trigger", {"hello": "world"}, max_age=60))

    assert set(store._entries) == {"trigger"}


def test_probabilistic_sweep_does_not_fire_below_threshold(monkeypatch) -> None:
    store = InMemorySessionStore()
    past = time.time() - 1
    # Well under the threshold — the sweep must not run even if the
    # dice always come up.
    for i in range(10):
        store._entries[f"expired-{i}"] = ({"i": i}, past)

    monkeypatch.setattr(sessions_module.random, "random", lambda: 0.0)

    _run(store.write("trigger", {"hello": "world"}, max_age=60))

    # Every expired entry survives — `write` only added "trigger".
    assert "trigger" in store._entries
    for i in range(10):
        assert f"expired-{i}" in store._entries


def test_probabilistic_sweep_skipped_when_dice_miss(monkeypatch) -> None:
    store = InMemorySessionStore()
    past = time.time() - 1
    n = sessions_module._SWEEP_THRESHOLD + 5
    for i in range(n):
        store._entries[f"expired-{i}"] = ({"i": i}, past)

    # 0.99 >= _SWEEP_PROBABILITY (~0.031), so the sweep must skip.
    monkeypatch.setattr(sessions_module.random, "random", lambda: 0.99)

    _run(store.write("trigger", {"hello": "world"}, max_age=60))

    # Nothing was evicted on this call.
    assert len(store._entries) == n + 1


def test_probabilistic_sweep_preserves_live_entries(monkeypatch) -> None:
    store = InMemorySessionStore()
    past = time.time() - 1
    future = time.time() + 3600
    n = sessions_module._SWEEP_THRESHOLD
    for i in range(n):
        store._entries[f"expired-{i}"] = ({"i": i}, past)
    store._entries["live-1"] = ({"v": 1}, future)
    store._entries["live-2"] = ({"v": 2}, future)

    monkeypatch.setattr(sessions_module.random, "random", lambda: 0.0)

    _run(store.write("trigger", {"hello": "world"}, max_age=60))

    # Sweep wiped every expired entry; the live ones (including the
    # one this `write` just added) survive.
    assert set(store._entries) == {"live-1", "live-2", "trigger"}


def test_probabilistic_sweep_fires_on_replace(monkeypatch) -> None:
    store = InMemorySessionStore()
    past = time.time() - 1
    n = sessions_module._SWEEP_THRESHOLD + 1
    for i in range(n):
        store._entries[f"expired-{i}"] = ({"i": i}, past)
    # The entry being replaced must itself be live, otherwise `replace`
    # short-circuits to `False` and the sweep is the only way to
    # observe the side effect.
    store._entries["target"] = ({"v": 0}, time.time() + 3600)

    monkeypatch.setattr(sessions_module.random, "random", lambda: 0.0)

    ok = _run(store.replace("target", {"v": 1}, max_age=60))

    assert ok is True
    assert set(store._entries) == {"target"}


def test_sweep_expired_tolerates_concurrent_removal() -> None:
    # A second caller pops one of the expired ids between snapshot and
    # eviction — `pop(..., None)` must swallow the miss, not raise KeyError.
    class _RacingDict(dict):
        racing_store: InMemorySessionStore | None = None
        racing_target: str | None = None
        triggered: bool = False

        def pop(self, key, *args, **kwargs):  # type: ignore[override]
            if (
                not _RacingDict.triggered
                and _RacingDict.racing_store is not None
                and _RacingDict.racing_target is not None
                and key != _RacingDict.racing_target
                and _RacingDict.racing_target in self
            ):
                _RacingDict.triggered = True
                super().pop(_RacingDict.racing_target, None)
            return super().pop(key, *args, **kwargs)

    store = InMemorySessionStore()
    racing = _RacingDict()
    store._entries = racing  # type: ignore[assignment]
    past = time.time() - 1
    store._entries["a"] = ({"v": 1}, past)
    store._entries["b"] = ({"v": 2}, past)
    store._entries["c"] = ({"v": 3}, past)
    _RacingDict.racing_store = store
    _RacingDict.racing_target = "b"

    removed = store.sweep_expired()

    # "a" and "c" evicted by the sweep; "b" disappeared concurrently.
    # All three were in the snapshot, so `pop(..., None)` must not raise.
    assert removed == 2
    assert dict(store._entries) == {}


def test_sweep_expired_tolerates_concurrent_write() -> None:
    # A second caller writes a brand-new id during iteration. The
    # snapshot taken at the top of `sweep_expired` must insulate the
    # loop from the live mutation — no `dictionary changed size`.
    class _RacingDict(dict):
        racing_store: InMemorySessionStore | None = None
        triggered: bool = False

        def items(self):  # type: ignore[override]
            view = super().items()
            if _RacingDict.racing_store is not None and not _RacingDict.triggered:
                _RacingDict.triggered = True
                # Mid-flight insertion mirroring a concurrent `write`.
                _RacingDict.racing_store._entries["fresh"] = (
                    {"v": 1},
                    time.time() + 3600,
                )
            return view

    store = InMemorySessionStore()
    racing = _RacingDict()
    store._entries = racing  # type: ignore[assignment]
    past = time.time() - 1
    for i in range(5):
        store._entries[f"expired-{i}"] = ({"i": i}, past)
    _RacingDict.racing_store = store

    removed = store.sweep_expired()

    assert removed == 5
    assert "fresh" in store._entries
    assert all(f"expired-{i}" not in store._entries for i in range(5))


def test_inmemory_session_store_sweep_expired_returns_count():
    store = InMemorySessionStore()
    now = time.time()
    store._entries["a"] = ({"x": 1}, now - 100)
    store._entries["b"] = ({"x": 2}, now - 50)
    store._entries["fresh"] = ({"x": 3}, now + 3600)
    removed = store.sweep_expired()
    assert removed == 2
    assert "fresh" in store._entries
    assert "a" not in store._entries
    assert "b" not in store._entries


def test_inmemory_session_store_sweep_tolerates_concurrent_removal():
    class RacingDict(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._racing = True

        def pop(self, key, default=None):
            if self._racing and key == "b":
                self._racing = False
                super().pop("b", None)
            return super().pop(key, default)

    store = InMemorySessionStore()
    now = time.time()
    store._entries = RacingDict({"a": ({}, now - 1), "b": ({}, now - 1), "c": ({}, now - 1)})
    removed = store.sweep_expired()
    assert removed == 2
    assert len(store._entries) == 0


async def test_in_memory_session_store_per_instance_sweep_fires() -> None:
    store = InMemorySessionStore(sweep_threshold=2, sweep_probability=1.0)
    # Pre-populate with three already-expired entries by reaching into the
    # internal map — public `write` would refresh the expiry.
    past = time.time() - 60
    store._entries["a"] = ({"x": 1}, past)
    store._entries["b"] = ({"x": 2}, past)
    store._entries["c"] = ({"x": 3}, past)
    assert len(store._entries) == 3

    # The next `write` should trip the per-instance sweep and evict the
    # three expired entries, leaving only the fresh one.
    await store.write("fresh", {"y": 1}, max_age=3600)

    assert set(store._entries) == {"fresh"}
