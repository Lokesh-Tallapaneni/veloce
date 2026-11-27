"""WebSocket.client / WebSocket.state accessors."""

from __future__ import annotations

from veloce.http.request import Address, State
from veloce.websocket import WebSocket


def _ws(client=None) -> WebSocket:
    scope = {"type": "websocket", "path": "/ws"}
    if client is not None:
        scope["client"] = client
    return WebSocket.from_asgi(scope, None, None)


# ── WebSocket.client ────────────────────────────────────────────────


def test_client_none_when_scope_lacks_it():
    assert _ws().client is None


def test_client_returns_address():
    ws = _ws(client=("203.0.113.5", 44321))
    assert ws.client == Address("203.0.113.5", 44321)
    assert ws.client.host == "203.0.113.5"
    assert ws.client.port == 44321


# ── WebSocket.state ─────────────────────────────────────────────────


def test_state_is_State_namespace():
    assert isinstance(_ws().state, State)


def test_state_attribute_storage():
    ws = _ws()
    ws.state.user = "alice"
    assert ws.state.user == "alice"
    assert ws.state["user"] == "alice"


def test_state_persists_across_accesses():
    ws = _ws()
    ws.state.count = 1
    # Same State object returned each time.
    assert ws.state is ws.state
    assert ws.state.count == 1
