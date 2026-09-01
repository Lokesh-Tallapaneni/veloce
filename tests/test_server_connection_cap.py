"""`MAX_CONCURRENT_CONNECTIONS`: refusing a connection over the cap, and
releasing the slot when one disconnects.

Split out of `test_polish_e2e.py`, a module named for a fix wave.
"""

from __future__ import annotations

import asyncio

from tests._protocol import _FakeTransport
from veloce import Veloce
from veloce.serving.protocol import HttpProtocol


def _reset_connection_counter() -> None:
    """Pin the class counter to 0 so test ordering doesn't leak state."""
    with HttpProtocol._connections_lock:
        HttpProtocol._active_connections = 0


def test_third_concurrent_connection_gets_503_and_closed():
    _reset_connection_counter()
    admitted: list[HttpProtocol] = []
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONCURRENT_CONNECTIONS"] = 2

        for _ in range(2):
            proto = HttpProtocol(app, loop)
            transport = _FakeTransport()
            proto.connection_made(transport)
            assert transport.closed is False
            assert proto._counted is True
            admitted.append(proto)

        assert HttpProtocol._active_connections == 2

        rejected = HttpProtocol(app, loop)
        rejected_transport = _FakeTransport()
        rejected.connection_made(rejected_transport)

        emitted = b"".join(rejected_transport.writes)
        assert b"HTTP/1.1 503" in emitted
        assert b"Service Unavailable" in emitted
        assert b"Connection: close" in emitted
        assert rejected_transport.closed is True
        assert rejected._counted is False
        assert HttpProtocol._active_connections == 2
    finally:
        for proto in admitted:
            proto.connection_lost(None)
        _reset_connection_counter()
        loop.close()


def test_disconnect_releases_slot_for_new_connection():
    _reset_connection_counter()
    proto_d: HttpProtocol | None = None
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONCURRENT_CONNECTIONS"] = 2

        proto_a = HttpProtocol(app, loop)
        proto_a.connection_made(_FakeTransport())
        proto_b = HttpProtocol(app, loop)
        proto_b.connection_made(_FakeTransport())
        assert HttpProtocol._active_connections == 2

        proto_a.connection_lost(None)
        proto_b.connection_lost(None)
        assert HttpProtocol._active_connections == 0

        proto_d = HttpProtocol(app, loop)
        d_transport = _FakeTransport()
        proto_d.connection_made(d_transport)

        assert d_transport.closed is False
        assert proto_d._counted is True
        assert HttpProtocol._active_connections == 1
        emitted = b"".join(d_transport.writes)
        assert b"503" not in emitted
    finally:
        if proto_d is not None:
            proto_d.connection_lost(None)
        _reset_connection_counter()
        loop.close()


# ── `None` means unlimited, which is what the documentation promises ──
#
# `docs/guide/configuration.md` says of `MAX_CONCURRENT_CONNECTIONS`: "Set
# `None` for unlimited". It did not do that. The admission check compared
# `_active_connections >= cap` with `cap` as `None`, which raises `TypeError` on
# every connection - so the one documented way to lift the cap refused *every*
# connection instead of admitting all of them, and did it silently: the client
# saw a handshake timeout, not an error naming the cause. Measured against the
# built-in server, `None` established 0 of 5000 connections.


def test_a_none_cap_admits_past_the_default():
    """The regression: `None` refused everything rather than nothing."""
    _reset_connection_counter()
    admitted: list[HttpProtocol] = []
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONCURRENT_CONNECTIONS"] = None

        for _ in range(5):
            proto = HttpProtocol(app, loop)
            transport = _FakeTransport()
            proto.connection_made(transport)
            assert transport.closed is False, "an uncapped server refused a connection"
            assert b"503" not in b"".join(transport.writes)
            admitted.append(proto)

        assert HttpProtocol._active_connections == 5
    finally:
        for proto in admitted:
            proto.connection_lost(None)
        _reset_connection_counter()
        loop.close()


def test_a_none_cap_still_counts_what_is_open():
    """Unlimited is not unmeasured - graceful shutdown reads the same counter."""
    _reset_connection_counter()
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONCURRENT_CONNECTIONS"] = None

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        assert proto._counted is True
        assert HttpProtocol._active_connections == 1

        proto.connection_lost(None)
        assert HttpProtocol._active_connections == 0
    finally:
        _reset_connection_counter()
        loop.close()


def test_a_cap_of_zero_still_refuses_everything():
    """`0` is a number, not a synonym for "unlimited"; only `None` is that."""
    _reset_connection_counter()
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONCURRENT_CONNECTIONS"] = 0

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        assert b"HTTP/1.1 503" in b"".join(transport.writes)
        assert transport.closed is True
    finally:
        _reset_connection_counter()
        loop.close()


def test_the_default_cap_is_unchanged_by_all_this():
    """The guard exists for a reason; lifting it must stay opt-in."""
    _reset_connection_counter()
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        assert app.config["MAX_CONCURRENT_CONNECTIONS"] == 1000
    finally:
        _reset_connection_counter()
        loop.close()
