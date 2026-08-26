"""Generated resolver source listings are registered, and bounded.

A compiled resolver has no file on disk, so its source is written into
`linecache` for the traceback machinery to find - otherwise a frame from it
prints `File "<veloce-resolver>", line N` with no source line.

The filename carries a digest of the source, so recompiling the same plan reuses
one entry and a static app's listings are bounded by its distinct resolvers.
A process that registers routes over its lifetime - per-tenant routers, a plugin
adding routes at runtime - had no such bound: every distinct handler shape
retained a full source listing, split into a list of lines, for the process
lifetime, for something only a traceback ever reads.

Registrations are now capped. These tests cover both directions, because a cache
that evicts too eagerly silently removes the debugging value the registration
exists for.
"""

from __future__ import annotations

import linecache

import pytest

from veloce import Depends, Veloce
from veloce._resolver_codegen import (
    _MAX_CACHED_SOURCES,
    _cached_source_keys,
    _register_source_key,
)


@pytest.fixture(autouse=True)
def _isolate_cache():
    """The registry is process-global; restore it so tests do not leak into
    each other or into the rest of the suite."""
    saved_keys = dict(_cached_source_keys)
    saved_lines = {k: linecache.cache[k] for k in saved_keys if k in linecache.cache}
    _cached_source_keys.clear()
    yield
    _cached_source_keys.clear()
    _cached_source_keys.update(saved_keys)
    for key, value in saved_lines.items():
        linecache.cache[key] = value


# ── the source is registered at all ──────────────────────────────────


def test_a_compiled_resolver_registers_its_source():
    """The behaviour the cap must not break."""
    app = Veloce(openapi_url=None)

    async def dep(q: int = 0):
        return q

    @app.get("/chained")
    async def chained(value: int = Depends(dep)):
        return {"value": value}

    from veloce.testclient import TestClient

    with TestClient(app) as client:
        client.get("/chained?q=1")

    ours = [k for k in linecache.cache if k.startswith("<veloce-")]
    assert ours, "no generated source was registered"
    filename = ours[0]
    assert linecache.getline(filename, 1) != ""


def test_a_registered_source_is_tracked_for_eviction():
    """A listing written but never tracked could never be evicted."""
    app = Veloce(openapi_url=None)

    @app.get("/q")
    async def q(a: int = 0):
        return {"a": a}

    from veloce.testclient import TestClient

    with TestClient(app) as client:
        client.get("/q?a=1")

    assert _cached_source_keys, "registrations are not tracked"
    for key in _cached_source_keys:
        assert key.startswith("<veloce-")


# ── and the registry is bounded ──────────────────────────────────────


def test_the_registry_stops_growing_at_the_cap():
    for index in range(_MAX_CACHED_SOURCES * 2):
        name = f"<veloce-test:{index}>"
        linecache.cache[name] = (0, None, [], name)
        _register_source_key(name)
    assert len(_cached_source_keys) == _MAX_CACHED_SOURCES


def test_the_oldest_entry_is_the_one_evicted():
    names = [f"<veloce-test:{i}>" for i in range(_MAX_CACHED_SOURCES + 3)]
    for name in names:
        linecache.cache[name] = (0, None, [], name)
        _register_source_key(name)
    assert names[0] not in _cached_source_keys
    assert names[-1] in _cached_source_keys


def test_an_evicted_entry_leaves_linecache_too():
    """Tracking without evicting from `linecache` would bound the wrong thing."""
    names = [f"<veloce-test:{i}>" for i in range(_MAX_CACHED_SOURCES + 2)]
    for name in names:
        linecache.cache[name] = (0, None, [], name)
        _register_source_key(name)
    assert names[0] not in linecache.cache


def test_eviction_only_touches_entries_this_module_wrote():
    """The negative that matters: a bound implemented over `linecache.cache`
    wholesale would evict other libraries' - and CPython's own - entries."""
    linecache.cache["<not-ours>"] = (0, None, [], "<not-ours>")
    try:
        for index in range(_MAX_CACHED_SOURCES + 5):
            name = f"<veloce-test:{index}>"
            linecache.cache[name] = (0, None, [], name)
            _register_source_key(name)
        assert "<not-ours>" in linecache.cache
    finally:
        linecache.cache.pop("<not-ours>", None)


def test_below_the_cap_nothing_is_evicted():
    """A static app must keep every listing it registered."""
    names = [f"<veloce-test:{i}>" for i in range(50)]
    for name in names:
        linecache.cache[name] = (0, None, [], name)
        _register_source_key(name)
    assert all(name in _cached_source_keys for name in names)
    assert all(name in linecache.cache for name in names)


def test_re_registering_the_same_key_does_not_grow_the_registry():
    """The digest already dedupes; the tracker must not undo that."""
    name = "<veloce-test:same>"
    linecache.cache[name] = (0, None, [], name)
    for _ in range(10):
        _register_source_key(name)
    assert len(_cached_source_keys) == 1
