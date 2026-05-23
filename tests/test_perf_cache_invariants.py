"""Cache-invariant regression tests for the profile-driven dispatch pass.

The perf rules in `.claude/rules/perf-changes.md` require callable-keyed
caches to use `WeakKeyDictionary`, and demand that any cache mirroring a
mutable structure invalidate at every mutation entry point. This module
pins:

- `Veloce._override_subplans` is a `WeakKeyDictionary` (structural).
- The `dependency_overrides` setter clears the cache so a wholesale
  reassignment rebuilds plans against the new mapping.
- `_exc_handler_sig_cache` is a `WeakKeyDictionary` (structural) and
  evicts pure-function handlers when they are unreferenced — the
  exception-handler sig flags are a plain tuple with no back-reference
  to the handler, so the WeakKey contract holds end-to-end here.

Note on the override-subplan side: `HandlerPlan` (the cache value)
holds strong references to the resolved callable inside its slot list,
so a WeakKey-on-the-callable cycle is not fully broken without also
weak-ref'ing the plan's callable refs. The intent of the WeakKey type
is still load-bearing — it cleans up entries when the *app* is the only
remaining ref-cycle root and lets the whole cycle collect together.
A stronger guarantee (per-entry eviction the moment user code releases
the override) is tracked as a follow-up. The setter `.clear()` covers
the common test pattern (rebind `app.dependency_overrides = {}`).
"""

from __future__ import annotations

import gc
import weakref

from veloce import Depends, Veloce
from veloce.testclient import TestClient


def test_override_subplans_is_weak_key_dictionary():
    """Structural pin: the cache type matches the rule."""
    app = Veloce(openapi_url=None)
    assert isinstance(app._override_subplans, weakref.WeakKeyDictionary)


def test_dependency_overrides_setter_clears_cache():
    """Assigning a fresh dict to `dependency_overrides` empties the cache
    so a future request rebuilds plans against the new mapping."""
    app = Veloce(openapi_url=None)

    def real():
        return "real"

    @app.get("/x")
    async def handler(value: str = Depends(real)) -> dict:
        return {"value": value}

    def fake_one():
        return "one"

    app.dependency_overrides[real] = fake_one
    TestClient(app).get("/x")
    assert fake_one in app._override_subplans

    # Wholesale reassignment must clear the cache, not just shadow it.
    app.dependency_overrides = {}
    assert len(app._override_subplans) == 0


def test_exc_handler_sig_cache_is_weak_key_dictionary():
    """Structural pin: the exception-handler signature flag cache uses
    weak keys, so a churning test suite that registers and tears down
    handlers doesn't accumulate dead entries."""
    from veloce.app import _exc_handler_sig_cache

    assert isinstance(_exc_handler_sig_cache, weakref.WeakKeyDictionary)


def test_exc_handler_sig_cache_evicts_on_handler_release():
    """The cache value is a `(bool, bool)` tuple with no back-reference
    to the handler — so WeakKey eviction works cleanly here even though
    it doesn't for `_override_subplans` (whose values back-ref via the
    plan)."""
    from veloce.app import _exc_handler_sig_cache

    def handler(request, exc):
        return None

    handler_ref = weakref.ref(handler)
    _exc_handler_sig_cache[handler] = (True, True)
    assert handler in _exc_handler_sig_cache

    del handler
    gc.collect()
    gc.collect()
    assert handler_ref() is None, "exception handler pinned by the sig cache"
