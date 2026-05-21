"""W1: ASGI websocket scope wiring + TestClient.websocket_connect."""

from __future__ import annotations

import pytest

from veloce import Veloce, WebSocketOriginMiddleware


def _make_app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    return app


def test_websocket_echo_text_via_test_client():
    app = _make_app()

    @app.websocket("/ws")
    async def echo(ws):
        await ws.accept()
        msg = await ws.receive_text()
        await ws.send_text(f"echo: {msg}")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "echo: hello"


def test_websocket_subprotocol_negotiation():
    app = _make_app()

    @app.websocket("/ws")
    async def neg(ws):
        chosen = ws.negotiate_subprotocol(["chat-v1", "chat-v2"])
        await ws.accept(subprotocol=chosen)
        await ws.send_text(chosen or "")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws", subprotocols=["chat-v2", "chat-v1"]) as ws:
        assert ws.accepted_subprotocol == "chat-v2"
        assert ws.receive_text() == "chat-v2"


def test_websocket_json_round_trip():
    app = _make_app()

    @app.websocket("/ws")
    async def js(ws):
        await ws.accept()
        data = await ws.receive_json()
        await ws.send_json({"echoed": data})
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        ws.send_text('{"hi": 1}')
        assert ws.receive_json() == {"echoed": {"hi": 1}}


def test_websocket_unknown_path_rejects():
    """No registered handler → ASGI close with code 1008."""
    app = _make_app()
    client = app.test_client()
    with (
        pytest.raises(RuntimeError, match="rejected with close code 1008"),
        client.websocket_connect("/missing"),
    ):
        pass


# ── S2: WebSocket Origin validation (CSWSH guard) ─────────────────────


def _origin_app() -> Veloce:
    app = _make_app()
    app.add_middleware(WebSocketOriginMiddleware(allowed_origins=["https://good.example"]))

    @app.websocket("/ws")
    async def echo(ws):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    return app


def test_websocket_allowed_origin_passes():
    client = _origin_app().test_client()
    with client.websocket_connect("/ws", headers={"origin": "https://good.example"}) as ws:
        assert ws.receive_text() == "hi"


def test_websocket_disallowed_origin_rejected():
    """A handshake from an unlisted Origin is refused (close code 1008)."""
    client = _origin_app().test_client()
    with (
        pytest.raises(RuntimeError, match="1008"),
        client.websocket_connect("/ws", headers={"origin": "https://evil.example"}),
    ):
        pass


def test_websocket_missing_origin_allowed_by_default():
    """Non-browser clients omit Origin; allow_missing defaults to True, so
    they still connect — browsers always send Origin, so CSWSH stays
    blocked."""
    client = _origin_app().test_client()
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "hi"


def test_websocket_origin_unit_check():
    mw = WebSocketOriginMiddleware(allowed_origins=["https://good.example"], allow_missing=False)
    assert mw.is_websocket_origin_allowed("https://good.example") is True
    assert mw.is_websocket_origin_allowed("https://evil.example") is False
    assert mw.is_websocket_origin_allowed("") is False
