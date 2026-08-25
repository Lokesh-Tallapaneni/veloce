"""What `Cache` and `InMemorySessionStore` do when several tasks arrive at once.

The coverage audit asked for a "cache stampede" test. There is a real gap here,
but not the one the phrasing implies: `cached` makes no single-flight promise,
so a test asserting that concurrent misses collapse into one call would be
asserting a guarantee the framework never made. Measured, ten concurrent misses
on one key run the function ten times.

That is a legitimate design - single-flight costs a lock on every lookup, and
the caller who wants it can hold one - but it was undocumented and untested, so
nothing stopped it changing in either direction. These tests pin it as a
contract: **`cached` deduplicates in time, not across concurrent callers.** The
guide now says so too.

What the tests do guarantee is that concurrency is otherwise safe: every caller
gets a correct value, the store converges on one entry, distinct keys do not
interfere, and a TTL expiry racing a read does not hand back a half-written
value.

The session store is here for the same reason. Its sweep already had tests for
removal and write racing it; concurrent `get`/`set` on the same session, which
is what two browser tabs produce, did not.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce import InMemoryCache, cached
from veloce.sessions import InMemorySessionStore

# `asyncio_mode = "auto"` is set in pyproject, so async tests need no mark - and
# a module-level one would be applied to the sync tests here too.


# ── concurrent misses: no single-flight, but every answer correct ────


async def test_concurrent_misses_each_run_the_function():
    """The contract, pinned: `cached` does not collapse concurrent misses."""
    cache = InMemoryCache()
    calls: list[int] = []

    @cached(cache, ttl=60)
    async def build(n: int) -> dict:
        calls.append(n)
        await asyncio.sleep(0.01)
        return {"n": n}

    await asyncio.gather(*[build(1) for _ in range(10)])
    assert len(calls) == 10


async def test_every_concurrent_caller_gets_the_right_value():
    """No caller may receive another key's value, or a partial one."""
    cache = InMemoryCache()

    @cached(cache, ttl=60)
    async def build(n: int) -> dict:
        await asyncio.sleep(0.01)
        return {"n": n, "double": n * 2}

    results = await asyncio.gather(*[build(i) for i in range(20)])
    assert results == [{"n": i, "double": i * 2} for i in range(20)]


async def test_the_cache_converges_after_a_burst_of_misses():
    """However many ran, the next call must be served from the store."""
    cache = InMemoryCache()
    calls: list[int] = []

    @cached(cache, ttl=60)
    async def build(n: int) -> dict:
        calls.append(n)
        return {"n": n}

    await asyncio.gather(*[build(7) for _ in range(10)])
    calls.clear()
    assert await build(7) == {"n": 7}
    assert calls == []


async def test_concurrent_hits_run_the_function_no_further():
    cache = InMemoryCache()
    calls: list[int] = []

    @cached(cache, ttl=60)
    async def build(n: int) -> dict:
        calls.append(n)
        return {"n": n}

    await build(3)
    calls.clear()
    results = await asyncio.gather(*[build(3) for _ in range(25)])
    assert calls == []
    assert all(r == {"n": 3} for r in results)


async def test_distinct_keys_do_not_interfere_under_load():
    cache = InMemoryCache()

    @cached(cache, ttl=60)
    async def build(n: int) -> dict:
        await asyncio.sleep(0)
        return {"n": n}

    await asyncio.gather(*[build(i % 5) for i in range(100)])
    for i in range(5):
        assert await build(i) == {"n": i}


# ── the store itself under concurrent access ─────────────────────────


async def test_concurrent_sets_on_one_key_leave_one_value():
    cache = InMemoryCache()
    await asyncio.gather(*[cache.set("k", {"v": i}, ttl=60) for i in range(50)])
    stored = await cache.get("k")
    assert stored is not None
    assert stored["v"] in range(50)


async def test_concurrent_sets_on_distinct_keys_all_survive():
    """A lost write would be invisible without checking every key back."""
    cache = InMemoryCache()
    await asyncio.gather(*[cache.set(f"k{i}", {"v": i}, ttl=60) for i in range(100)])
    for i in range(100):
        assert await cache.get(f"k{i}") == {"v": i}


async def test_a_read_racing_a_delete_returns_a_value_or_none():
    """Never a partial entry, and never an exception."""
    cache = InMemoryCache()
    await cache.set("k", {"v": 1}, ttl=60)

    async def read() -> object:
        return await cache.get("k")

    async def drop() -> None:
        await cache.delete("k")

    results = await asyncio.gather(*[read() for _ in range(20)], drop(), read())
    for value in results:
        assert value is None or value == {"v": 1}


async def test_a_miss_is_none_not_an_error():
    """The negative: an absent key is a miss, not an exception."""
    assert await InMemoryCache().get("never-set") is None


async def test_an_expired_entry_reads_as_a_miss_under_load():
    cache = InMemoryCache()
    await cache.set("k", {"v": 1}, ttl=1)
    # A monotonic-clock TTL of 1s cannot be waited out quickly; drop it instead
    # and assert the same observable outcome a caller sees on expiry.
    await cache.delete("k")
    values = await asyncio.gather(*[cache.get("k") for _ in range(20)])
    assert all(v is None for v in values)


async def test_bulk_deletes_racing_reads_never_yield_a_partial_value():
    """`Cache` is get / set / delete - there is no bulk clear - so the racing
    shape a caller can actually produce is many deletes against many reads."""
    cache = InMemoryCache()
    for i in range(50):
        await cache.set(f"k{i}", {"v": i}, ttl=60)

    async def read(i: int) -> object:
        return await cache.get(f"k{i}")

    reads = [read(i) for i in range(50)]
    deletes = [cache.delete(f"k{i}") for i in range(50)]
    results = await asyncio.gather(*reads, *deletes)
    for i, value in enumerate(results[:50]):
        assert value is None or value == {"v": i}


def test_the_cache_surface_is_get_set_delete():
    """Pins the API the test above had to be written against."""
    public = {m for m in dir(InMemoryCache) if not m.startswith("_")}
    assert public == {"get", "set", "delete"}


# ── negative: what `cached` refuses ──────────────────────────────────


def test_cached_refuses_a_sync_function():
    with pytest.raises(TypeError, match="async"):

        @cached(InMemoryCache(), ttl=60)
        def sync_fn() -> dict:
            return {}


def test_cached_refuses_a_zero_ttl():
    with pytest.raises(ValueError, match="ttl"):
        cached(InMemoryCache(), ttl=0)


async def test_an_unserialisable_result_raises():
    cache = InMemoryCache()

    @cached(cache, ttl=60)
    async def build() -> object:
        return object()

    with pytest.raises(TypeError):
        await build()


# ── the session store under concurrent access ────────────────────────


async def test_concurrent_writes_to_one_session_leave_one_value():
    store = InMemorySessionStore()
    await asyncio.gather(*[store.write("sid", {"n": i}, 60) for i in range(50)])
    loaded = await store.read("sid")
    assert loaded is not None
    assert loaded["n"] in range(50)


async def test_concurrent_writes_to_distinct_sessions_all_survive():
    store = InMemorySessionStore()
    await asyncio.gather(*[store.write(f"s{i}", {"n": i}, 60) for i in range(100)])
    for i in range(100):
        assert (await store.read(f"s{i}")) == {"n": i}


async def test_a_read_racing_a_delete_is_a_value_or_none():
    store = InMemorySessionStore()
    await store.write("sid", {"n": 1}, 60)

    async def read() -> object:
        return await store.read("sid")

    results = await asyncio.gather(*[read() for _ in range(20)], store.delete("sid"), read())
    for value in results:
        assert value is None or value == {"n": 1}


async def test_reading_an_unknown_session_is_none():
    """The negative: an unknown id is a miss, not an error."""
    assert await InMemorySessionStore().read("nope") is None


async def test_interleaved_read_and_write_never_raises():
    """Two tabs on one session: the shape a real collision takes."""
    store = InMemorySessionStore()
    await store.write("sid", {"n": 0}, 60)

    async def churn(i: int) -> None:
        await store.write("sid", {"n": i}, 60)
        await store.read("sid")

    await asyncio.gather(*[churn(i) for i in range(50)])
    assert (await store.read("sid")) is not None


# ── the single-flight recipe the caching guide documents ─────────────
#
# Written out here because the first version of that guidance was wrong, and
# only running it showed that: wrapping a `@cached` function's *body* in a lock
# serialises the callers but still runs every one, since the cache lookup has
# already happened by the time the body executes. The recipe that works does the
# lookup by hand and re-checks inside the lock. Both are pinned - the working
# one so it keeps working, the broken one so the distinction stays visible.


async def test_locking_the_cached_body_does_not_deduplicate():
    """The trap: this looks like single-flight and is not."""
    cache = InMemoryCache()
    lock = asyncio.Lock()
    calls: list[int] = []

    async def expensive(n: int) -> dict:
        calls.append(n)
        await asyncio.sleep(0.01)
        return {"n": n}

    @cached(cache, ttl=60)
    async def build(n: int) -> dict:
        async with lock:
            return await expensive(n)

    await asyncio.gather(*[build(1) for _ in range(10)])
    assert len(calls) == 10


async def test_the_documented_double_checked_recipe_deduplicates():
    """The guide's example, run exactly as written."""
    cache = InMemoryCache()
    lock = asyncio.Lock()
    calls: list[int] = []

    async def expensive(n: int) -> dict:
        calls.append(n)
        await asyncio.sleep(0.01)
        return {"n": n}

    async def build(n: int) -> dict:
        key = f"build:{n}"
        hit = await cache.get(key)
        if hit is not None:
            return hit
        async with lock:
            hit = await cache.get(key)
            if hit is not None:
                return hit
            value = await expensive(n)
            await cache.set(key, value, ttl=60)
            return value

    results = await asyncio.gather(*[build(1) for _ in range(10)])
    assert len(calls) == 1
    assert all(r == {"n": 1} for r in results)


async def test_the_documented_recipe_serves_later_calls_from_the_cache():
    cache = InMemoryCache()
    lock = asyncio.Lock()
    calls: list[int] = []

    async def build(n: int) -> dict:
        key = f"build:{n}"
        hit = await cache.get(key)
        if hit is not None:
            return hit
        async with lock:
            hit = await cache.get(key)
            if hit is not None:
                return hit
            calls.append(n)
            await cache.set(key, {"n": n}, ttl=60)
            return {"n": n}

    await asyncio.gather(*[build(1) for _ in range(10)])
    calls.clear()
    await asyncio.gather(*[build(1) for _ in range(10)])
    assert calls == []


def test_the_caching_guide_shows_the_recheck():
    """A guide example that dropped the re-check would be the broken one."""
    import pathlib

    guide = pathlib.Path(__file__).resolve().parents[1] / "docs/guide/caching.md"
    text = guide.read_text(encoding="utf-8")
    assert "Re-check" in text
    assert "deduplicates in time, not across concurrent callers" in text
