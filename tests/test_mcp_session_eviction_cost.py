"""Resolving an MCP session does not walk every other live session.

`HttpSessionStore.resolve` runs on every MCP request, and it began by sweeping
idle sessions with a full scan of the live map. The cost of serving one
conversation therefore grew with the number of other conversations open:

    resolve with    10 live sessions   0.90 us
    resolve with   100 live sessions   4.26 us
    resolve with  1000 live sessions  37.45 us

A session store is exactly the structure that gets large in the case it exists
for, so this was a scan on the hot path in proportion to load.

Touching a session now re-inserts it, which costs nothing extra (the timestamp
was already being written) and leaves `_live` ordered oldest-touched first. The
sweep walks only the entries it actually reclaims and stops at the first live
one:

    resolve with    10 live sessions   0.65 us
    resolve with   100 live sessions   0.65 us
    resolve with  1000 live sessions   0.66 us

Flat, and faster even at ten. The eviction behaviour is unchanged, including
`idle_ttl=0` ("evict on next access"), which these tests pin.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from veloce.contrib.mcp.transports.session_store import HttpSessionStore

# ── eviction still evicts ────────────────────────────────────────────


async def test_an_idle_session_is_reclaimed():
    evicted = []
    store = HttpSessionStore(idle_ttl=0, on_evict=evicted.append)
    session_id, _session = await store.create()
    await asyncio.sleep(0.02)
    assert await store.resolve(session_id) is None
    assert len(evicted) == 1


async def test_a_fresh_session_is_not_reclaimed():
    store = HttpSessionStore(idle_ttl=3600)
    session_id, session = await store.create()
    assert await store.resolve(session_id) is session


async def test_resolving_refreshes_the_deadline():
    """An actively-used session is never reclaimed."""
    store = HttpSessionStore(idle_ttl=0.05)
    session_id, session = await store.create()
    for _ in range(4):
        await asyncio.sleep(0.02)
        assert await store.resolve(session_id) is session


async def test_only_the_idle_sessions_are_reclaimed():
    """The early break must not stop before reclaiming everything expired."""
    evicted = []
    store = HttpSessionStore(idle_ttl=0.05, on_evict=evicted.append)
    old = [(await store.create())[0] for _ in range(5)]
    await asyncio.sleep(0.08)
    fresh_id, fresh_session = await store.create()
    assert await store.resolve(fresh_id) is fresh_session
    assert len(evicted) == 5
    for session_id in old:
        assert await store.resolve(session_id) is None


async def test_a_touched_session_survives_while_its_older_siblings_go():
    """The reorder is what the sweep's early break depends on."""
    evicted = []
    store = HttpSessionStore(idle_ttl=0.05, on_evict=evicted.append)
    first_id, first_session = await store.create()
    others = [(await store.create())[0] for _ in range(3)]
    for _ in range(4):
        await asyncio.sleep(0.02)
        await store.resolve(first_id)
    assert await store.resolve(first_id) is first_session
    assert len(evicted) == 3
    for session_id in others:
        assert await store.resolve(session_id) is None


async def test_an_unknown_id_resolves_to_none():
    store = HttpSessionStore(idle_ttl=3600)
    assert await store.resolve("never-minted") is None


async def test_the_eviction_callback_receives_the_session():
    evicted = []
    store = HttpSessionStore(idle_ttl=0, on_evict=evicted.append)
    _session_id, session = await store.create()
    await asyncio.sleep(0.02)
    await store.create()
    assert evicted == [session]


async def test_no_callback_is_fine():
    """`on_evict=None` is the default; eviction must not require one."""
    store = HttpSessionStore(idle_ttl=0)
    session_id, _session = await store.create()
    await asyncio.sleep(0.02)
    assert await store.resolve(session_id) is None


# ── the cost no longer tracks the number of live sessions ────────────


async def _resolve_cost(store: HttpSessionStore, session_id: str) -> float:
    """Best-of-5 microseconds for one `resolve`.

    Awaited inside one coroutine rather than driven by `run_until_complete` per
    call: the loop's per-iteration overhead is tens of microseconds and would
    swamp the very thing being measured. An earlier version of this test did
    exactly that and passed against the unfixed code.
    """
    best = []
    for _ in range(5):
        start = time.perf_counter()
        for _ in range(500):
            await store.resolve(session_id)
        best.append((time.perf_counter() - start) / 500 * 1e6)
    return min(best)


@pytest.mark.parametrize("count", [200, 1000])
async def test_resolution_does_not_slow_down_with_more_live_sessions(count):
    """A full scan made this grow linearly; the ratio pinned here is generous.

    The threshold is deliberately loose (4x for a 20x and 100x increase in
    sessions) so the test pins the *shape* - flat, not linear - rather than a
    machine-specific number. Before the fix the 1000-session ratio was ~41x.
    """
    small = HttpSessionStore(idle_ttl=3600)
    small_ids = [(await small.create())[0] for _ in range(10)]
    baseline = await _resolve_cost(small, small_ids[5])

    large = HttpSessionStore(idle_ttl=3600)
    large_ids = [(await large.create())[0] for _ in range(count)]
    loaded = await _resolve_cost(large, large_ids[count // 2])

    assert loaded < baseline * 4, f"{loaded:.2f}us at {count} sessions vs {baseline:.2f}us at 10"


async def test_a_sweep_of_many_expired_sessions_reclaims_them_all():
    """The early break must not leave a backlog behind."""
    evicted = []
    store = HttpSessionStore(idle_ttl=0.05, on_evict=evicted.append)
    for _ in range(200):
        await store.create()
    await asyncio.sleep(0.08)
    await store.create()
    assert len(evicted) == 200
