"""Signals primitive + framework integration."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce
from veloce.signals import (
    Signal,
    got_request_exception,
    request_finished,
    request_started,
    request_tearing_down,
)


def _req(path: str = "/x") -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── Signal class ─────────────────────────────────────────────────────


def test_connect_and_send():
    sig = Signal("test")
    calls = []

    def handler(sender, **kwargs):
        calls.append((sender, kwargs))

    sig.connect(handler, weak=False)
    sig.send("sender-x", foo=1)
    assert calls == [("sender-x", {"foo": 1})]


def test_disconnect_stops_firing():
    sig = Signal("test")
    calls = []

    def handler(sender, **kwargs):
        calls.append(1)

    sig.connect(handler, weak=False)
    sig.disconnect(handler)
    sig.send("x")
    assert calls == []


def test_has_receivers_for_true_when_connected():
    sig = Signal()
    assert not sig.has_receivers_for(None)
    sig.connect(lambda s, **kw: None, weak=False)
    assert sig.has_receivers_for(None)


def test_weak_ref_dies_when_owner_collected():
    """Weak-ref receivers don't pin the owner."""
    import gc

    sig = Signal()

    class Owner:
        def handle(self, sender, **kw):
            pass

    o = Owner()
    sig.connect(o.handle, weak=True)
    assert sig.has_receivers_for(None)
    del o
    gc.collect()
    # After GC, the weakref's target is gone; send sees zero receivers.
    sig.send("x")
    assert not sig.has_receivers_for(None)


# ── Framework integration ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_started_fires_on_dispatch():
    app = Veloce(debug=True, openapi_url=None)
    fired = []

    def on_start(sender, **kw):
        fired.append(("started", sender))

    request_started.connect(on_start, weak=False)
    try:

        @app.get("/x")
        async def x():
            return {}

        await app.handle_request(_req())
        assert fired and fired[0] == ("started", app)
    finally:
        request_started.disconnect(on_start)


@pytest.mark.asyncio
async def test_request_finished_fires_with_response():
    app = Veloce(debug=True, openapi_url=None)
    captured = []

    def on_done(sender, **kw):
        captured.append(kw.get("response"))

    request_finished.connect(on_done, weak=False)
    try:

        @app.get("/x")
        async def x():
            return {"ok": True}

        resp = await app.handle_request(_req())
        assert captured == [resp]
    finally:
        request_finished.disconnect(on_done)


@pytest.mark.asyncio
async def test_request_tearing_down_always_fires():
    """Even on a clean request, teardown signal fires."""
    app = Veloce(debug=True, openapi_url=None)
    fired = []

    def on_tear(sender, **kw):
        fired.append(kw.get("exc"))

    request_tearing_down.connect(on_tear, weak=False)
    try:

        @app.get("/x")
        async def x():
            return {}

        await app.handle_request(_req())
        assert fired == [None]
    finally:
        request_tearing_down.disconnect(on_tear)


@pytest.mark.asyncio
async def test_got_request_exception_fires_on_error():
    app = Veloce(debug=True, openapi_url=None)
    seen = []

    def on_exc(sender, **kw):
        seen.append(kw.get("exception"))

    got_request_exception.connect(on_exc, weak=False)
    try:

        @app.get("/boom")
        async def boom():
            raise RuntimeError("kaboom")

        # Veloce converts to 500; the signal still fires.
        await app.handle_request(_req("/boom"))
        assert seen and isinstance(seen[0], RuntimeError)
    finally:
        got_request_exception.disconnect(on_exc)


# ── send vs send_robust: per-receiver failure handling ──────────────


def test_send_aborts_on_failing_receiver():
    sig = Signal("strict")
    fired: list[str] = []

    def good_a(sender, **kwargs):
        fired.append("a")
        return "a"

    def bad(sender, **kwargs):
        fired.append("bad")
        raise RuntimeError("boom")

    def good_b(sender, **kwargs):
        fired.append("b")
        return "b"

    sig.connect(good_a, weak=False)
    sig.connect(bad, weak=False)
    sig.connect(good_b, weak=False)

    with pytest.raises(RuntimeError, match="boom"):
        sig.send("s")

    # Strict semantics: receivers registered after `bad` never ran.
    assert fired == ["a", "bad"]


def test_send_robust_returns_exceptions_and_continues():
    sig = Signal("robust")
    fired: list[str] = []

    def good_a(sender, **kwargs):
        fired.append("a")
        return "a"

    def bad(sender, **kwargs):
        fired.append("bad")
        raise RuntimeError("boom")

    def good_b(sender, **kwargs):
        fired.append("b")
        return "b"

    sig.connect(good_a, weak=False)
    sig.connect(bad, weak=False)
    sig.connect(good_b, weak=False)

    results = sig.send_robust("s")

    # Every receiver ran, in order.
    assert fired == ["a", "bad", "b"]
    assert len(results) == 3
    assert results[0] == (good_a, "a")
    assert results[1][0] is bad
    assert isinstance(results[1][1], RuntimeError)
    assert str(results[1][1]) == "boom"
    assert results[2] == (good_b, "b")


def test_send_robust_rejects_async_receiver_with_typeerror():
    """Sync send_robust + async receiver → TypeError entry, no unawaited coroutine."""
    import warnings

    sig = Signal("sync-only")

    async def async_handler(sender, **kwargs):
        return "async"

    sig.connect(async_handler, weak=False)

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        results = sig.send_robust("s")

    assert len(results) == 1
    receiver, value = results[0]
    assert receiver is async_handler
    assert isinstance(value, TypeError)
    assert "send_robust_async" in str(value)


async def test_send_robust_async_mixed_sync_async_with_failure():
    """Async send_robust runs every receiver; per-receiver errors are captured."""
    sig = Signal("mixed")
    fired: list[str] = []

    def sync_ok(sender, **kwargs):
        fired.append("sync")
        return "sync-value"

    async def async_bad(sender, **kwargs):
        fired.append("async-bad")
        raise RuntimeError("kaboom")

    async def async_ok(sender, **kwargs):
        fired.append("async-ok")
        return "async-value"

    sig.connect(sync_ok, weak=False)
    sig.connect(async_bad, weak=False)
    sig.connect(async_ok, weak=False)

    results = await sig.send_robust_async("s")

    assert fired == ["sync", "async-bad", "async-ok"]
    assert len(results) == 3
    assert results[0] == (sync_ok, "sync-value")
    assert results[1][0] is async_bad
    assert isinstance(results[1][1], RuntimeError)
    assert str(results[1][1]) == "kaboom"
    assert results[2] == (async_ok, "async-value")


def test_iter_live_targets_prunes_dead_weakref_after_single_send():
    """Both send and send_robust prune dead weakrefs via _iter_live_targets."""
    import gc

    class Owner:
        def handle(self, sender, **kw):
            return "ok"

    # send() path
    sig_a = Signal("prune-send")
    keep = Owner()
    drop = Owner()
    sig_a.connect(keep.handle, weak=True)
    sig_a.connect(drop.handle, weak=True)
    assert len(sig_a._subs) == 2
    del drop
    gc.collect()
    sig_a.send("x")
    assert len(sig_a._subs) == 1

    # send_robust() path
    sig_b = Signal("prune-robust")
    keep2 = Owner()
    drop2 = Owner()
    sig_b.connect(keep2.handle, weak=True)
    sig_b.connect(drop2.handle, weak=True)
    assert len(sig_b._subs) == 2
    del drop2
    gc.collect()
    sig_b.send_robust("x")
    assert len(sig_b._subs) == 1


def test_send_robust_logs_failures(caplog):
    sig = Signal("robust-log")

    def bad(sender, **kwargs):
        raise ValueError("nope")

    sig.connect(bad, weak=False)

    with caplog.at_level("WARNING", logger="veloce.signals"):
        results = sig.send_robust("s")

    assert len(results) == 1
    assert isinstance(results[0][1], ValueError)
    assert any(
        rec.name == "veloce.signals" and rec.levelname == "WARNING" for rec in caplog.records
    )
    # The traceback (exc_info) must be attached so operators can debug.
    assert any(rec.exc_info for rec in caplog.records)
