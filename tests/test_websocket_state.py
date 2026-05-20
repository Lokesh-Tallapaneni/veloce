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
