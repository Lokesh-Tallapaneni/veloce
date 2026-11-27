"""An empty `InMemorySessionStore` is falsy, and that is now a stated contract.

Adding `__len__` to a public class changes its truthiness. Before it, every
instance was truthy; after it, an empty store is falsy - so user code writing
`if store:` to mean "a store is configured" takes the wrong branch whenever the
store is empty, which is to say at process start.

The semantics themselves are right: the class also gained `__contains__` and
`__iter__`, so it presents as a collection, and empty-is-falsy is what every
other collection in Python does. Forcing `__bool__` to `True` would leave
`len(store) == 0` beside a truthy store, which is the more surprising pair.

What was wrong is that it shipped as an `### Added` line about `len()`, `in` and
iteration with nothing said about the truthiness that came with them. These tests
pin the whole contract so the next reader finds it stated rather than inferred.
"""

from __future__ import annotations

import pytest

from veloce.sessions import InMemorySessionStore


async def _store(count: int = 0) -> InMemorySessionStore:
    store = InMemorySessionStore()
    for i in range(count):
        await store.write(f"sid-{i}", {"n": i}, 3600)
    return store


async def test_an_empty_store_is_falsy():
    """The collection reading, stated so it is a decision and not a surprise."""
    assert not await _store()


async def test_a_populated_store_is_truthy():
    assert await _store(1)


async def test_truthiness_follows_the_length():
    """`__bool__` is not defined, so this is the only thing that can be true."""
    store = await _store(2)

    assert bool(store) is (len(store) > 0)


async def test_a_store_that_empties_becomes_falsy():
    store = await _store(1)
    assert store

    store.clear()

    assert not store


async def test_is_not_none_still_answers_configured():
    """The test in-tree callers use, and the migration for `if store:`."""
    store = await _store()

    assert store is not None
    assert not store


async def test_an_expired_entry_does_not_keep_the_store_truthy():
    """Truthiness follows the live count, matching `__len__`'s own contract."""
    store = InMemorySessionStore()
    await store.write("sid", {"n": 1}, -1)

    assert len(store) == 0
    assert not store


@pytest.mark.parametrize("count", [1, 2, 10])
async def test_any_live_session_makes_it_truthy(count: int):
    assert await _store(count)
