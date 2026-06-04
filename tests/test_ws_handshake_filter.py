"""WebSocket handshake host / origin gate - pre-filtered pipeline equivalence.

The handshake host / origin allow-lists are pre-filtered from the registered
middleware into a frozen tuple at compile time, so the per-connect path no longer
probes every middleware. These tests pin that the ALLOW / DENY outcomes (and the
1008 close code) are identical through the pre-filtered path, that the build helper
filters correctly, and that an app with no host/origin middleware takes the empty
fast path.
"""

from __future__ import annotations

import pytest

from veloce import TrustedHostMiddleware, Veloce, WebSocketOriginMiddleware
from veloce._pipeline import build_ws_handshake_checks
from veloce.middleware.base import Middleware


def _ws_app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)

    @app.websocket("/ws")
    async def echo(ws):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    return app


# ── Origin gate (CSWSH guard) through the pre-filtered tuple ──────────


def test_origin_allow_passes():
    app = _ws_app()
    app.add_middleware(WebSocketOriginMiddleware(allowed_origins=["https://good.example"]))
    client = app.test_client()
    with client.websocket_connect("/ws", headers={"origin": "https://good.example"}) as ws:
        assert ws.receive_text() == "hi"


def test_origin_deny_closes_1008():
    app = _ws_app()
    app.add_middleware(WebSocketOriginMiddleware(allowed_origins=["https://good.example"]))
    client = app.test_client()
    with (
        pytest.raises(RuntimeError, match="1008"),
        client.websocket_connect("/ws", headers={"origin": "https://evil.example"}),
    ):
        pass


# ── Host gate through the pre-filtered tuple ─────────────────────────


def test_host_allow_passes():
    app = _ws_app()
    app.add_middleware(TrustedHostMiddleware(allowed_hosts=["good.example"]))
    client = app.test_client()
    with client.websocket_connect("/ws", headers={"host": "good.example"}) as ws:
        assert ws.receive_text() == "hi"


def test_host_deny_closes_1008():
    app = _ws_app()
    app.add_middleware(TrustedHostMiddleware(allowed_hosts=["good.example"]))
    client = app.test_client()
    with (
        pytest.raises(RuntimeError, match="1008"),
        client.websocket_connect("/ws", headers={"host": "evil.example"}),
    ):
        pass


# ── Empty fast path - middleware exists but exposes no handshake check ─


class _NoCheckMiddleware(Middleware):
    """A middleware with neither host nor origin handshake check."""


def test_no_handshake_check_middleware_connects():
    """A middleware without host/origin checks must not gate the handshake."""
    app = _ws_app()
    app.add_middleware(_NoCheckMiddleware())
    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "hi"


def test_no_middleware_skips_gate():
    """With no middleware at all, the handshake slot is `None` (gate skipped)."""
    app = _ws_app()
    cp = app._ensure_pipeline()
    assert cp.ws_handshake is None
    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "hi"


# ── build_ws_handshake_checks unit equivalence ───────────────────────


def test_build_filters_only_checked_middleware():
    """Only middleware exposing a host/origin check appears, in order, with the
    other slot `None` - mirroring the per-connect `getattr(..., None)` probe."""
    app = _ws_app()
    host_mw = TrustedHostMiddleware(allowed_hosts=["good.example"])
    origin_mw = WebSocketOriginMiddleware(allowed_origins=["https://good.example"])
    app.add_middleware(_NoCheckMiddleware())
    app.add_middleware(host_mw)
    app.add_middleware(origin_mw)

    pairs = build_ws_handshake_checks(app)
    # The check-free middleware is filtered out; the two checked ones survive
    # in registration order.
    assert len(pairs) == 2
    assert pairs[0] == (host_mw.is_host_allowed, None)
    assert pairs[1] == (None, origin_mw.is_websocket_origin_allowed)


def test_build_empty_when_no_checked_middleware():
    app = _ws_app()
    app.add_middleware(_NoCheckMiddleware())
    assert build_ws_handshake_checks(app) == ()
