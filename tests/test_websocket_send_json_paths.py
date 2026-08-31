"""`send_json` produces the same frame on both transports, without transcoding twice.

`send_json(mode="text")` encoded the payload to UTF-8 bytes, decoded them to a
`str`, and handed that to `send_text` - which encoded them straight back. On the
ASGI path the decode is unavoidable (the `websocket.send` event carries a
`str`), but on the native transport it was a full decode/encode round trip of
every message for nothing.

The native path now frames the bytes directly. These tests assert the two
transports emit the same JSON, including for payloads where a careless
bytes/str change would show up first: non-ASCII text, characters outside the
BMP, and an empty document.
"""

from __future__ import annotations

import json

import pytest

from tests._native_ws import mark_accepted
from veloce import Veloce
from veloce.exceptions import WebSocketDisconnect
from veloce.testclient import TestClient
from veloce.websocket import WebSocket

PAYLOADS = [
    {"ok": True},
    {"text": "plain ascii"},
    {"text": "café ünïcode"},
    {"text": "emoji \U0001f600 outside the BMP"},
    {"nested": {"a": [1, 2, {"b": None}]}},
    {},
    {"n": 12345, "f": 1.5},
    {"text": 'a quote " and a trailing backslash ' + chr(92)},
]


def _app(payload):
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def json_route(websocket):
        await websocket.accept()
        await websocket.send_json(payload)
        await websocket.close()

    return app


@pytest.mark.parametrize("payload", PAYLOADS)
def test_send_json_round_trips_over_the_asgi_path(payload):
    with TestClient(_app(payload)).websocket_connect("/ws") as ws:
        assert ws.receive_json() == payload


@pytest.mark.parametrize("payload", PAYLOADS)
def test_send_json_round_trips_over_the_native_path(payload):
    """The path the change touches: no ASGI send callable, so frames are built
    by the raw framer."""
    from tests._native_ws import native_ws_json

    assert native_ws_json(payload) == payload


@pytest.mark.parametrize("payload", PAYLOADS)
def test_both_transports_send_the_same_json(payload):
    """Stated as the property rather than asserted twice."""
    from tests._native_ws import native_ws_json

    with TestClient(_app(payload)).websocket_connect("/ws") as ws:
        asgi = ws.receive_json()
    assert asgi == native_ws_json(payload)


def test_binary_mode_is_unaffected():
    app = Veloce(openapi_url=None)
    payload = {"text": "café"}

    @app.websocket("/ws")
    async def binary_route(websocket):
        await websocket.accept()
        await websocket.send_json(payload, mode="binary")
        await websocket.close()

    with TestClient(app).websocket_connect("/ws") as socket:
        assert json.loads(socket.receive_bytes()) == payload


def test_an_invalid_mode_is_refused():
    app = Veloce(openapi_url=None)
    seen = []

    @app.websocket("/ws")
    async def bad_mode_route(websocket):
        await websocket.accept()
        try:
            await websocket.send_json({"a": 1}, mode="nope")
        except ValueError as exc:
            seen.append(str(exc))
        await websocket.close()

    with TestClient(app).websocket_connect("/ws"):
        pass
    assert seen and "mode must be" in seen[0]


# ── the native branch keeps the guards `send_text` applied ───────────
#
# The native path no longer goes through `send_text`, so it has to enforce the
# same preconditions itself. Nothing else covered them on this branch: removing
# both left every test above green.


def _native_ws():
    from tests._native_ws import _KEY, _RecordingTransport

    return WebSocket(_RecordingTransport(), dict(_KEY))


async def test_send_json_before_accept_is_refused_on_the_native_path():
    ws = _native_ws()
    with pytest.raises(RuntimeError, match="accept"):
        await ws.send_json({"a": 1})


async def test_send_json_after_close_is_refused_on_the_native_path():

    ws = mark_accepted(_native_ws())
    ws._closed = True
    with pytest.raises(WebSocketDisconnect):
        await ws.send_json({"a": 1})


async def test_send_json_before_accept_is_refused_in_binary_mode_too():
    """`send_bytes` owns that guard on the binary branch; asserted so the two
    modes cannot drift."""
    ws = _native_ws()
    with pytest.raises(RuntimeError, match="accept"):
        await ws.send_json({"a": 1}, mode="binary")
