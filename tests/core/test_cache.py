"""Result caching — InMemoryCache, the cached decorator, and handler integration."""

from __future__ import annotations

import time

import pytest
from pydantic import BaseModel

from veloce import Cache, InMemoryCache, Request, Veloce, cached
from veloce.testclient import TestClient

# ── InMemoryCache ────────────────────────────────────────────────────


async def test_set_get_round_trip():
    cache = InMemoryCache()
    await cache.set("k", b"value", 60)
    assert await cache.get("k") == b"value"


async def test_get_missing_returns_none():
    assert await InMemoryCache().get("absent") is None


async def test_delete_removes_entry():
    cache = InMemoryCache()
    await cache.set("k", b"v", 60)
    await cache.delete("k")
    assert await cache.get("k") is None


async def test_expired_entry_is_evicted_on_get():
    cache = InMemoryCache()
    await cache.set("k", b"v", 60)
    # Force the entry's monotonic expiry into the past, then read it back.
    cache._entries["k"] = (b"v", time.monotonic() - 1)
    assert await cache.get("k") is None
    assert "k" not in cache._entries


async def test_bounded_eviction_drops_oldest():
    cache = InMemoryCache(max_entries=2)
    await cache.set("a", b"1", 60)
    await cache.set("b", b"2", 60)
    await cache.set("c", b"3", 60)  # over capacity -> oldest ("a") evicted
    assert await cache.get("a") is None
    assert await cache.get("b") == b"2"
    assert await cache.get("c") == b"3"


def test_max_entries_must_be_positive():
    with pytest.raises(ValueError, match="max_entries"):
        InMemoryCache(max_entries=0)


# ── cached decorator ─────────────────────────────────────────────────


async def test_cached_returns_value_and_skips_recompute():
    cache = InMemoryCache()
    calls = 0

    @cached(cache, ttl=60)
    async def compute(x: int) -> dict:
        nonlocal calls
        calls += 1
        return {"x": x}

    assert await compute(1) == {"x": 1}
    assert await compute(1) == {"x": 1}  # served from cache
    assert calls == 1
    assert await compute(2) == {"x": 2}  # different arg -> recomputed
    assert calls == 2


async def test_cached_default_key_ignores_unserialisable_args():
    """A handler keyed by its scalar inputs ignores an injected request object,
    so two calls with the same input but different request objects share a hit."""
    cache = InMemoryCache()
    calls = 0

    class _Req:
        pass

    @cached(cache, ttl=60)
    async def handler(item_id: int, request: object) -> dict:
        nonlocal calls
        calls += 1
        return {"id": item_id}

    await handler(1, _Req())
    await handler(1, _Req())  # different unserialisable arg, same item_id
    assert calls == 1


async def test_cached_explicit_key():
    cache = InMemoryCache()
    calls = 0

    @cached(cache, ttl=60, key=lambda user_id: f"user:{user_id}")
    async def load(user_id: int) -> dict:
        nonlocal calls
        calls += 1
        return {"id": user_id}

    await load(7)
    await load(7)
    assert calls == 1
    assert await cache.get("user:7") is not None


async def test_cached_pydantic_result_round_trips_as_dict():
    cache = InMemoryCache()

    class Out(BaseModel):
        a: int
        b: str

    @cached(cache, ttl=60)
    async def make() -> Out:
        return Out(a=1, b="x")

    first = await make()
    second = await make()  # hit -> JSON-decoded
    assert first.model_dump() == {"a": 1, "b": "x"}
    assert second == {"a": 1, "b": "x"}


async def test_cached_unserialisable_result_raises():
    cache = InMemoryCache()

    @cached(cache, ttl=60)
    async def bad() -> object:
        return object()

    with pytest.raises(TypeError, match="not JSON-serialisable"):
        await bad()


def test_cached_requires_async():
    with pytest.raises(TypeError, match="async"):

        @cached(InMemoryCache(), ttl=60)
        def sync_fn() -> dict:
            return {}


def test_cached_rejects_bad_ttl():
    with pytest.raises(ValueError, match="ttl"):
        cached(InMemoryCache(), ttl=0)


# ── Handler integration ──────────────────────────────────────────────


def test_cached_handler_served_from_cache():
    app = Veloce(openapi_url=None)
    cache = InMemoryCache()
    hits = 0

    @app.get("/items/{item_id}")
    @cached(cache, ttl=60)
    async def get_item(item_id: int, request: Request) -> dict:
        nonlocal hits
        hits += 1
        return {"item_id": item_id}

    with TestClient(app) as tc:
        assert tc.get("/items/5").json() == {"item_id": 5}
        assert tc.get("/items/5").json() == {"item_id": 5}
        assert hits == 1  # second request hit the cache
        assert tc.get("/items/9").json() == {"item_id": 9}
        assert hits == 2


async def test_cache_base_class_methods_are_abstract():
    base = Cache()
    with pytest.raises(NotImplementedError):
        await base.get("k")
    with pytest.raises(NotImplementedError):
        await base.set("k", b"v", 60)
    with pytest.raises(NotImplementedError):
        await base.delete("k")
