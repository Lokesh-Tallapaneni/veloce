"""WebSocketState enum + application_state / client_state."""

from __future__ import annotations

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


def test_send_oserror_normalized_to_disconnect():
    import asyncio

    import pytest

    from veloce import WebSocketDisconnect

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        if message.get("type") == "websocket.send":
            raise BrokenPipeError("peer gone")

    ws = WebSocket.from_asgi({"type": "websocket", "path": "/", "headers": []}, receive, send)
    ws._accepted = True

    async def run():
        with pytest.raises(WebSocketDisconnect):
            await ws.send_text("hi")
        assert ws._closed is True

    asyncio.new_event_loop().run_until_complete(run())


def test_send_bytes_connectionreset_normalized():
    import asyncio

    import pytest

    from veloce import WebSocketDisconnect

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        raise ConnectionResetError("reset")

    ws = WebSocket.from_asgi({"type": "websocket", "path": "/", "headers": []}, receive, send)
    ws._accepted = True

    async def run():
        with pytest.raises(WebSocketDisconnect):
            await ws.send_bytes(b"x")

    asyncio.new_event_loop().run_until_complete(run())
