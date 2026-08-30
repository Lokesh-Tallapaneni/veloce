"""WebSocketState enum + application_state / client_state."""

from __future__ import annotations

import veloce
from veloce import Veloce
from veloce.testclient import TestClient
from veloce.websocket import WebSocket, WebSocketState


def test_the_state_enum_is_importable_from_the_package_root():
    """It is the declared return type of two public `WebSocket` properties.

    A user annotating against `ws.application_state` needs the name, so it is
    exported. This asserts the package-root import itself - the version it
    replaces compared two names bound by one `veloce.websocket` import, which no
    source change could have made fail.
    """
    # The import *is* the assertion - the test is named for it, and moving
    # it to module top would move the failure to collection.
    from veloce import WebSocketState as FromRoot

    assert FromRoot is WebSocketState
    assert "WebSocketState" in veloce.__all__


def test_state_enum_values():
    assert WebSocketState.CONNECTING == 0
    assert WebSocketState.CONNECTED == 1
    assert WebSocketState.DISCONNECTED == 2


def test_state_progression_through_lifecycle():
    app = Veloce()
    observations: list[tuple[WebSocketState, WebSocketState]] = []

    @app.websocket("/x")
    async def h(ws: WebSocket):
        observations.append((ws.application_state, ws.client_state))
        await ws.accept()
        observations.append((ws.application_state, ws.client_state))
        await ws.close()
        observations.append((ws.application_state, ws.client_state))

    with TestClient(app) as client, client.websocket_connect("/x"):
        pass

    assert observations == [
        (WebSocketState.CONNECTING, WebSocketState.CONNECTING),
        (WebSocketState.CONNECTED, WebSocketState.CONNECTED),
        (WebSocketState.DISCONNECTED, WebSocketState.DISCONNECTED),
    ]


def test_state_disconnected_after_peer_close():
    app = Veloce()
    final: list[WebSocketState] = []

    @app.websocket("/y")
    async def h(ws: WebSocket):
        await ws.accept()
        async for _ in ws.iter_text():
            pass
        # Peer closed — iter_text returned cleanly; state should reflect that.
        final.append(ws.application_state)

    with TestClient(app) as client, client.websocket_connect("/y"):
        pass

    # iter_* helpers swallow WebSocketDisconnect, but the underlying
    # receive_text marks `_closed = True` before raising. State is
    # therefore DISCONNECTED once the loop exits.
    assert final == [WebSocketState.DISCONNECTED]


# -- `ws.state` scratch namespace + slotted connection object ---------


def test_state_namespace_holds_per_connection_data():
    app = Veloce()
    seen: list[tuple[str, str]] = []

    @app.websocket("/s")
    async def h(ws: WebSocket):
        await ws.accept()
        ws.state.user = await ws.receive_text()
        ws.state["room"] = "lobby"
        seen.append((ws.state.user, ws.state["room"]))

    with TestClient(app) as client:
        with client.websocket_connect("/s") as ws:
            ws.send_text("ada")
        with client.websocket_connect("/s") as ws:
            ws.send_text("grace")

    # Each connection gets its own namespace rather than sharing one.
    assert seen == [("ada", "lobby"), ("grace", "lobby")]


def test_connection_rejects_undeclared_attributes():
    """`WebSocket` is slotted, so application data goes on `ws.state`."""
    import pytest

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        pass

    ws = WebSocket.from_asgi({"type": "websocket", "path": "/", "headers": []}, receive, send)

    assert not hasattr(ws, "__dict__")
    with pytest.raises(AttributeError):
        ws.user = "ada"


def test_both_constructors_initialise_every_slot():
    """A field added to one construction path only would surface as an
    `AttributeError` at runtime, so both paths must fill every slot."""

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        pass

    class _Transport:
        def write(self, data): ...
        def close(self): ...
        def get_extra_info(self, name, default=None):
            return default

    asgi = WebSocket.from_asgi({"type": "websocket", "path": "/", "headers": []}, receive, send)
    native = WebSocket.from_transport(
        _Transport(),
        {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="},
        {"type": "websocket", "path": "/", "headers": []},
    )

    for name in WebSocket.__slots__:
        assert hasattr(asgi, name), f"{name} unset on the ASGI path"
        assert hasattr(native, name), f"{name} unset on the raw-transport path"
