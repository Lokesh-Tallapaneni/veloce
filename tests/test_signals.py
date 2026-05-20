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
