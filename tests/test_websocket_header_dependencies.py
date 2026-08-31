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

from veloce import Cookie, Depends, Header, Query, Veloce, WebSocket
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


# ── list-typed markers ───────────────────────────────────────────────
#
# The scalar case above was fixed by `0fb2d81`, and the tests it added cover
# only scalars - so the list-typed branch was left broken behind a suite that
# looks like it covers the area. That branch reads `.getlist(...)`, which a
# `Request`'s multi-dict headers and cookies have and a `WebSocket`'s plain
# handshake dicts do not, so the handler never ran and the socket closed 1011.


def test_a_list_typed_header_resolves_on_a_websocket_route():
    """The regression: `AttributeError: 'dict' object has no attribute 'getlist'`."""
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket, tags: list[str] = Header(default_factory=list)) -> None:
        await ws.accept()
        await ws.send_text(",".join(tags))
        await ws.close()

    with TestClient(app).websocket_connect("/ws", headers={"tags": "alpha"}) as session:
        assert session.receive_text() == "alpha"


def test_a_list_typed_header_falls_back_to_its_default():
    """An absent header must reach the default, not raise."""
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket, tags: list[str] = Header(default_factory=list)) -> None:
        await ws.accept()
        await ws.send_text(repr(tags))
        await ws.close()

    with TestClient(app).websocket_connect("/ws") as session:
        assert session.receive_text() == "[]"


def test_a_list_typed_cookie_resolves_on_a_websocket_route():
    """`Cookie` takes the same branch, off the same plain dict."""
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket, seen: list[str] = Cookie(default_factory=list)) -> None:
        await ws.accept()
        await ws.send_text(",".join(seen))
        await ws.close()

    client = TestClient(app)
    with client.websocket_connect("/ws", headers={"Cookie": "seen=one"}) as session:
        assert session.receive_text() == "one"


def test_a_list_typed_query_param_still_resolves():
    """The control: `query_params` is already a multi-dict on both transports."""
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket, ids: list[str] = Query(default_factory=list)) -> None:
        await ws.accept()
        await ws.send_text(",".join(ids))
        await ws.close()

    with TestClient(app).websocket_connect("/ws?ids=a&ids=b") as session:
        assert session.receive_text() == "a,b"


def test_a_list_typed_header_still_resolves_over_http():
    """The HTTP side of the same branch, so the fix cannot break the door it worked on.

    A single value here rather than repeats: `TestClient` takes a header mapping,
    which folds duplicates before they reach the app. Repeated headers over HTTP
    are covered in `tests/test_header_list_marker.py`, which drives raw ASGI
    header tuples to get more than one of the same name onto the wire.
    """
    app = Veloce(openapi_url=None)

    @app.get("/probe")
    async def probe(tags: list[str] = Header(default_factory=list)) -> dict:
        return {"tags": tags}

    assert TestClient(app).get("/probe", headers={"tags": "a"}).json()["tags"] == ["a"]


def test_a_typed_list_header_is_coerced_on_a_websocket_route():
    """Coercion runs on this branch too, so it must survive the read."""
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket, nums: list[int] = Header(default_factory=list)) -> None:
        await ws.accept()
        await ws.send_text(repr(nums))
        await ws.close()

    with TestClient(app).websocket_connect("/ws", headers={"nums": "7"}) as session:
        assert session.receive_text() == "[7]"
