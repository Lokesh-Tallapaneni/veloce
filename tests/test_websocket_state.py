"""WebSocketState enum + application_state / client_state."""

from __future__ import annotations

from tests._native_ws import mark_accepted
from veloce import Veloce
from veloce.testclient import TestClient
from veloce.websocket import WebSocket, WebSocketState


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


def test_state_importable_from_top_level():
    from veloce.websocket import WebSocketState as TopState

    assert TopState is WebSocketState


# -- OSError normalization on the ASGI send path ----------------------


async def test_send_oserror_normalized_to_disconnect():

    import pytest

    from veloce import WebSocketDisconnect

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        if message.get("type") == "websocket.send":
            raise BrokenPipeError("peer gone")

    ws = mark_accepted(
        WebSocket.from_asgi({"type": "websocket", "path": "/", "headers": []}, receive, send)
    )

    async def run():
        with pytest.raises(WebSocketDisconnect):
            await ws.send_text("hi")
        assert ws._closed is True

    await run()


async def test_send_bytes_connectionreset_normalized():

    import pytest

    from veloce import WebSocketDisconnect

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        raise ConnectionResetError("reset")

    ws = mark_accepted(
        WebSocket.from_asgi({"type": "websocket", "path": "/", "headers": []}, receive, send)
    )

    async def run():
        with pytest.raises(WebSocketDisconnect):
            await ws.send_bytes(b"x")

    await run()


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
