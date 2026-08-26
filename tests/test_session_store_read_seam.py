"""Reading `InMemorySessionStore` without reaching into its dict.

The store's public surface is all writes and one read that copies a payload:
`read`, `write`, `delete`, `replace`, `touch`, `sweep_expired`. Three things
tests and operators legitimately want are not on it - how many sessions are
live, whether one particular id is, and when an id expires - so the suite read
them off `store._entries` at dozens of sites across the session modules.

The third is not a convenience. Sliding expiry refreshes a TTL *without changing
the payload*, so `read` returns the same dict before and after and cannot show
that `touch` did anything. The expiry stamp is the only observable, and it had
no accessor at all.

`expires_at`, `__contains__`, `__len__` and `__iter__` close that. All four
agree with `read` about what "present" means: an entry past its expiry is absent
whether or not the lazy sweep has reached it yet.
"""

from __future__ import annotations

import time

from veloce.sessions import InMemorySessionStore


async def _store(**entries: int) -> InMemorySessionStore:
    """A store seeded with `id=max_age` pairs. A negative age seeds an expired
    entry - `write` computes `now + max_age`, so no private access is needed."""
    store = InMemorySessionStore()
    for session_id, max_age in entries.items():
        await store.write(session_id, {"v": session_id}, max_age)
    return store


# ── expires_at ───────────────────────────────────────────────────────


async def test_expires_at_returns_the_stamp():
    store = await _store(live=60)
    assert store.expires_at("live") is not None
    assert store.expires_at("live") > time.time()


async def test_expires_at_is_none_for_an_absent_id():
    assert (await _store()).expires_at("nope") is None


async def test_expires_at_is_none_for_an_expired_entry():
    """Consistent with `read`, which treats a stale entry as gone."""
    store = await _store(stale=-5)
    assert await store.read("stale") is None
    assert store.expires_at("stale") is None


async def test_expires_at_moves_forward_when_the_ttl_is_refreshed():
    """The observable sliding expiry has and the payload does not."""
    store = await _store(sid=60)
    before = store.expires_at("sid")
    assert before is not None
    await store.touch("sid", 3600)
    after = store.expires_at("sid")
    assert after is not None
    assert after > before


async def test_the_payload_is_unchanged_by_a_refresh():
    """Why the stamp is needed: `read` cannot see a `touch`."""
    store = await _store(sid=60)
    before = await store.read("sid")
    await store.touch("sid", 3600)
    assert await store.read("sid") == before


async def test_a_rewrite_also_moves_the_stamp():
    store = await _store(sid=60)
    before = store.expires_at("sid")
    assert before is not None
    await store.write("sid", {"v": "new"}, 3600)
    after = store.expires_at("sid")
    assert after is not None
    assert after > before


# ── membership ───────────────────────────────────────────────────────


async def test_a_live_id_is_in_the_store():
    assert "live" in await _store(live=60)


async def test_an_absent_id_is_not():
    assert "nope" not in await _store(live=60)


async def test_an_expired_id_is_not():
    assert "stale" not in await _store(stale=-5)


async def test_a_deleted_id_is_not():
    store = await _store(sid=60)
    await store.delete("sid")
    assert "sid" not in store


async def test_a_non_string_is_not_in_the_store():
    """`__contains__` takes `object`; it must answer rather than raise."""
    assert 7 not in await _store(live=60)
    assert None not in await _store(live=60)


# ── count and iteration ──────────────────────────────────────────────


async def test_len_counts_live_sessions():
    assert len(await _store(a=60, b=60, c=60)) == 3


async def test_len_excludes_expired_entries():
    """The count is the live one even before a sweep runs."""
    store = await _store(a=60, b=-5, c=-5)
    assert len(store) == 1


async def test_len_of_an_empty_store_is_zero():
    assert len(await _store()) == 0


async def test_counting_does_not_evict():
    """A read-side accessor must not have a write's side effect."""
    store = await _store(a=60, b=-5)
    len(store)
    assert store.sweep_expired() == 1


async def test_iteration_yields_the_live_ids():
    assert sorted(await _store(a=60, b=60)) == ["a", "b"]


async def test_iteration_excludes_expired_ids():
    assert list(await _store(a=60, b=-5)) == ["a"]


async def test_iteration_is_insertion_ordered():
    assert list(await _store(z=60, a=60, m=60)) == ["z", "a", "m"]


async def test_iterating_is_safe_against_concurrent_deletion():
    """The ids are materialised, so deleting during the loop cannot raise."""
    store = await _store(a=60, b=60, c=60)
    seen = []
    for session_id in store:
        seen.append(session_id)
        await store.delete(session_id)
    assert sorted(seen) == ["a", "b", "c"]
    assert len(store) == 0


# ── the four accessors agree with each other and with `read` ─────────


async def test_the_accessors_agree_on_a_live_entry():
    store = await _store(sid=60)
    assert await store.read("sid") is not None
    assert "sid" in store
    assert store.expires_at("sid") is not None
    assert list(store) == ["sid"]
    assert len(store) == 1


async def test_the_accessors_agree_on_an_expired_entry():
    store = await _store(sid=-5)
    assert await store.read("sid") is None
    assert "sid" not in store
    assert store.expires_at("sid") is None
    assert list(store) == []
    assert len(store) == 0


# ── clear ────────────────────────────────────────────────────────────
#
# The one write the suite reached in for: a synchronous "revoke everything",
# which is what a key rotation or a breach needs and `delete` (async, one id)
# could not express from a sync test.


async def test_clear_empties_the_store():
    store = await _store(a=60, b=60)
    store.clear()
    assert len(store) == 0
    assert await store.read("a") is None


async def test_clear_returns_how_many_were_removed():
    assert (await _store(a=60, b=60, c=60)).clear() == 3


async def test_clear_counts_expired_entries_it_removed():
    """They were occupying the store whether or not a sweep had reached them."""
    assert (await _store(a=60, b=-5)).clear() == 2


async def test_clearing_an_empty_store_removes_nothing():
    assert (await _store()).clear() == 0


async def test_the_store_is_reusable_after_clearing():
    store = await _store(a=60)
    store.clear()
    await store.write("b", {"v": 1}, 60)
    assert await store.read("b") == {"v": 1}
    assert len(store) == 1
