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

**The flatness is asserted as a count, not a duration.** This module used to
time 1,000 resolutions and compare a 4x ratio, in the default suite - which is
the class of test this project excludes behind the `perf` marker, and which
flakes on a loaded machine. What the fix actually changed is the *length of the
sweep*, and that is exactly countable: it stops at the first live entry, so one
resolve visits one entry whatever the store holds. Counting it states the
property precisely, keeps it in the default suite, and catches strictly more -
the count assertions fail even at 10 sessions, where the ratio test could not
distinguish a full scan from a bounded one at all.
"""

from __future__ import annotations

import asyncio

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


class _CountingMap(dict):
    """A `_live` map that records how many entries the sweep walks.

    The cost this module is about is the *length of the sweep*, and that is a
    count, not a duration - the sweep stops at the first live entry, so it
    visits one entry whatever the store holds. Counting it states the property
    exactly and cannot flake on a loaded machine, which a wall-clock ratio can.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.walked = 0

    def items(self):
        for pair in super().items():
            self.walked += 1
            yield pair


async def _walk_length(store: HttpSessionStore, session_id: str) -> int:
    """Entries the idle sweep visits during one `resolve`."""
    counting = _CountingMap(store._live)
    store._live = counting
    counting.walked = 0
    await store.resolve(session_id)
    return counting.walked


async def _store_with(count: int) -> tuple[HttpSessionStore, list[str]]:
    store = HttpSessionStore(idle_ttl=3600)
    ids = [(await store.create())[0] for _ in range(count)]
    return store, ids


@pytest.mark.parametrize("count", [10, 200, 1000])
async def test_resolution_walks_one_entry_whatever_the_store_holds(count):
    """The defect: a full scan walked every live session on every request.

    Asserted as a count rather than a duration. The previous version of this
    test timed 1,000 resolutions against a 4x ratio and ran in the default
    suite, which is the class of test this project excludes behind the `perf`
    marker - and a machine-independent count makes the marker unnecessary
    rather than merely moving the test out of everyone's way.
    """
    store, ids = await _store_with(count)
    assert await _walk_length(store, ids[count // 2]) == 1


async def test_the_walk_length_is_the_same_at_ten_and_at_a_thousand():
    """Stated as the comparison the old ratio test was reaching for, but exact."""
    small, small_ids = await _store_with(10)
    large, large_ids = await _store_with(1000)
    assert await _walk_length(small, small_ids[5]) == await _walk_length(large, large_ids[500])


async def test_the_walk_is_bounded_by_what_it_reclaims_not_by_the_store():
    """The mechanism, checked directly: with everything expired the sweep walks
    the whole store, because every entry is being reclaimed - and with nothing
    expired it walks one. The cost tracks the reclaim, not the population."""
    store, ids = await _store_with(50)
    assert await _walk_length(store, ids[25]) == 1

    # Built with a long TTL so the entries accumulate, then expired together -
    # a store created with `idle_ttl=0` reclaims on every `create` and never
    # holds more than one.
    expiring, expiring_ids = await _store_with(50)
    expiring._idle_ttl = 0
    assert await _walk_length(expiring, expiring_ids[25]) == 50


async def test_a_sweep_of_many_expired_sessions_reclaims_them_all():
    """The early break must not leave a backlog behind."""
    evicted = []
    store = HttpSessionStore(idle_ttl=0.05, on_evict=evicted.append)
    for _ in range(200):
        await store.create()
    await asyncio.sleep(0.08)
    await store.create()
    assert len(evicted) == 200


# ── the live session count has a ceiling ─────────────────────────────
#
# Eviction by idle TTL bounds how *long* a session lives, not how many exist at
# once. Minting one costs a client a single request, so within one TTL window a
# caller could accumulate them without limit. `max_sessions` is that ceiling.
#
# At capacity the store reclaims the least-recently-touched session rather than
# refusing the new one: `_live` is ordered oldest-touched-first, so the victim is
# the entry least likely to be in use, and a flood cannot lock legitimate clients
# out of the transport entirely.


async def test_the_live_count_stops_at_the_ceiling():
    store = HttpSessionStore(idle_ttl=3600, max_sessions=25)
    for _ in range(200):
        await store.create()
    assert len(store._live) <= 25


async def test_a_new_session_is_still_minted_at_capacity():
    """Refusing would let a flood lock real clients out."""
    store = HttpSessionStore(idle_ttl=3600, max_sessions=5)
    for _ in range(10):
        await store.create()
    session_id, session = await store.create()
    assert await store.resolve(session_id) is session


async def test_the_least_recently_touched_session_is_the_victim():
    store = HttpSessionStore(idle_ttl=3600, max_sessions=3)
    first, _ = await store.create()
    second, _ = await store.create()
    third, _ = await store.create()

    # Touch the oldest so it is no longer the least recent.
    await store.resolve(first)
    await store.create()

    assert await store.resolve(first) is not None
    assert await store.resolve(second) is None


async def test_an_evicted_session_reaches_the_evict_callback():
    """The transport reclaims what a session owned; a capacity eviction must
    not skip that the way an idle reclaim does not."""
    evicted = []
    store = HttpSessionStore(idle_ttl=3600, on_evict=evicted.append, max_sessions=2)
    for _ in range(5):
        await store.create()
    assert len(evicted) >= 3


async def test_a_ceiling_below_one_is_refused():
    with pytest.raises(ValueError, match="max_sessions"):
        HttpSessionStore(max_sessions=0)


async def test_the_default_ceiling_does_not_disturb_ordinary_use():
    """The negative: a bound set too low would evict sessions a normal
    deployment is still using."""
    store = HttpSessionStore(idle_ttl=3600)
    ids = [(await store.create())[0] for _ in range(500)]
    for session_id in ids:
        assert await store.resolve(session_id) is not None
