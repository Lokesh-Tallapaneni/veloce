"""A WebSocket handler reads headers through the same dependencies HTTP does.

The resolver passes a `WebSocket` wherever an HTTP resolve passes a `Request`,
so anything that reads a header off "the connection" reaches the WebSocket on a
`ws` route: a `Header()` parameter, and every security scheme that inspects
`Authorization`. `docs/guide/websockets.md` documents `Depends()` auth on
WebSocket handlers, so these are supported shapes.

Nothing covered them, which is how a change that read headers off `Request`
alone could pass the whole suite while closing every authenticated WebSocket
handshake with a 1011.
"""

from __future__ import annotations

import base64

import pytest

from veloce import Depends, Header, Veloce, WebSocket
from veloce.security import APIKeyHeader, HTTPBasic, HTTPBearer
from veloce.testclient import TestClient

_BASIC = base64.b64encode(b"alice:secret").decode()


def _shown(credential: object) -> str:
    """What a scheme handed back, whether that is a value or a credential object."""
    for attribute in ("credentials", "username"):
        value = getattr(credential, attribute, None)
        if value:
            return str(value)
    return str(credential) if credential else "absent"


@pytest.fixture
def client() -> TestClient:
    app = Veloce(openapi_url=None)
    bearer = HTTPBearer(auto_error=False)
    basic = HTTPBasic(auto_error=False)
    api_key = APIKeyHeader(name="X-API-Key", auto_error=False)

    @app.websocket("/header")
    async def header_param(ws: WebSocket, x_token: str = Header(default="absent")) -> None:
        await ws.accept()
        await ws.send_text(x_token)
        await ws.close()

    @app.websocket("/bearer")
    async def bearer_auth(ws: WebSocket, cred: object = Depends(bearer)) -> None:
        await ws.accept()
        await ws.send_text(_shown(cred))
        await ws.close()

    @app.websocket("/basic")
    async def basic_auth(ws: WebSocket, cred: object = Depends(basic)) -> None:
        await ws.accept()
        await ws.send_text(_shown(cred))
        await ws.close()

    @app.websocket("/apikey")
    async def api_key_auth(ws: WebSocket, key: str = Depends(api_key)) -> None:
        await ws.accept()
        await ws.send_text(key or "absent")
        await ws.close()

    return TestClient(app)


def _talk(client: TestClient, path: str, headers: dict[str, str]) -> str:
    with client.websocket_connect(path, headers=headers) as session:
        return session.receive_text()


@pytest.mark.parametrize(
    ("path", "headers", "expected"),
    [
        ("/header", {"X-Token": "secret-token"}, "secret-token"),
        ("/bearer", {"Authorization": "Bearer tok-123"}, "tok-123"),
        ("/basic", {"Authorization": f"Basic {_BASIC}"}, "alice"),
        ("/apikey", {"X-API-Key": "k_live_9"}, "k_live_9"),
    ],
)
def test_a_websocket_handler_reads_the_header(client, path, headers, expected):
    """The handshake completes and the value arrives - not a 1011 close."""
    assert _talk(client, path, headers) == expected


@pytest.mark.parametrize("path", ["/header", "/bearer", "/basic", "/apikey"])
def test_an_absent_header_is_not_an_error(client, path):
    """`auto_error=False` and a defaulted `Header()` both mean "carry on"."""
    assert _talk(client, path, {}) == "absent"


def test_the_header_lookup_is_case_insensitive(client):
    """A client may send any casing; the handshake dict is keyed lowercase."""
    assert _talk(client, "/header", {"X-TOKEN": "shouty"}) == "shouty"


def test_every_scheme_agrees_across_both_transports():
    """The same credential must read the same over HTTP and over WebSocket.

    Two schemes reading one `Authorization` header should not disagree because
    of which door the request came through.
    """
    app = Veloce(openapi_url=None)
    bearer = HTTPBearer(auto_error=False)

    @app.get("/probe")
    async def over_http(cred: object = Depends(bearer)) -> dict:
        return {"token": _shown(cred)}

    @app.websocket("/probe-ws")
    async def over_ws(ws: WebSocket, cred: object = Depends(bearer)) -> None:
        await ws.accept()
        await ws.send_text(_shown(cred))
        await ws.close()

    client = TestClient(app)
    header = {"Authorization": "Bearer same-token"}
    http_value = client.get("/probe", headers=header).json()["token"]
    with client.websocket_connect("/probe-ws", headers=header) as session:
        ws_value = session.receive_text()
    assert http_value == ws_value == "same-token"
