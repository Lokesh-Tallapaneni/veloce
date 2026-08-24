"""A refused WebSocket handshake refuses the same way on both transports.

The two transports refused in a different order and with a different result.
The built-in server matched the route first, so an unknown path drew 404 and a
disallowed origin drew 403; the ASGI path gated host/Origin first and answered
both with a `1008` close, which a server renders as 403.

Two things were wrong. Matching first tells an origin the app has already
decided not to trust whether a path exists, which is an information leak; and
one request drew a different status depending on how the app was served, so a
client or a monitoring rule could not tell "no such endpoint" from "not allowed
here".

Both now gate before matching, and the ASGI path answers an unknown path with a
real 404 wherever the server advertises the `websocket.http.response` extension
(uvicorn does, on both its implementations). Without the extension it falls back
to the spec's close, which is the only refusal that transport can make.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce import Veloce
from veloce.middleware.security import WebSocketOriginMiddleware

_ALLOWED = "https://good.example"
_EVIL = "https://evil.example"


def _app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(WebSocketOriginMiddleware(allowed_origins=[_ALLOWED]))

    @app.websocket("/ws")
    async def echo(ws):
        await ws.accept()
        await ws.close()

    return app


def _drive(path: str, origin: str | None, *, extension: bool) -> list[dict]:
    """Run one ASGI websocket handshake and return what the app sent."""
    scope: dict = {
        "type": "websocket",
        "path": path,
        "query_string": b"",
        "headers": [(b"host", b"testserver")] + ([(b"origin", origin.encode())] if origin else []),
        "root_path": "",
        "scheme": "ws",
    }
    if extension:
        scope["extensions"] = {"websocket.http.response": {}}
    sent: list[dict] = []
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "websocket.connect"}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(_app()(scope, receive, send))
    return sent


# ── The gate runs before the route table ─────────────────────────────


@pytest.mark.parametrize("path", ["/ws", "/nope"])
def test_a_disallowed_origin_is_refused_whether_or_not_the_path_exists(path):
    """Matching first told a refused origin which paths exist."""
    sent = _drive(path, _EVIL, extension=True)
    assert sent[0]["type"] == "websocket.close"
    assert sent[0]["code"] == 1008


def test_a_disallowed_origin_draws_the_same_refusal_for_both_paths():
    """The leak: the refusal must not vary by whether the route is registered."""
    known = _drive("/ws", _EVIL, extension=True)
    unknown = _drive("/nope", _EVIL, extension=True)
    assert known == unknown


# ── An unknown path says so, where the server can carry it ───────────


def test_an_unknown_path_answers_404_when_the_server_supports_it():
    """The defect: it was indistinguishable from a policy refusal."""
    sent = _drive("/nope", _ALLOWED, extension=True)
    assert sent[0]["type"] == "websocket.http.response.start"
    assert sent[0]["status"] == 404


def test_an_unknown_path_falls_back_to_the_spec_close_without_the_extension():
    """The fallback must remain the spec's refusal, not an error."""
    sent = _drive("/nope", _ALLOWED, extension=False)
    assert sent[0]["type"] == "websocket.close"
    assert sent[0]["code"] == 1008


def test_a_policy_refusal_stays_a_close_even_where_a_status_could_be_sent():
    """A refused origin is not a missing endpoint; it keeps the 1008 close."""
    sent = _drive("/ws", _EVIL, extension=True)
    assert sent[0]["type"] == "websocket.close"


# ── An allowed handshake is unaffected ───────────────────────────────


def test_an_allowed_origin_on_a_known_path_still_connects():
    sent = _drive("/ws", _ALLOWED, extension=True)
    assert sent[0]["type"] == "websocket.accept"


# ── The built-in server gates in the same order ──────────────────────


class _FakeTransport(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name, default=None):
        return default


def _native(path: str, origin: str | None) -> bytes:
    """Drive one raw handshake through the built-in server."""
    from veloce.serving.protocol import HttpProtocol

    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(_app(), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)
        request = (
            f"GET {path} HTTP/1.1\r\nHost: testserver\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
            + (f"Origin: {origin}\r\n" if origin else "")
            + "\r\n"
        )
        proto.data_received(request.encode())
        loop.run_until_complete(asyncio.sleep(0))
        written = b"".join(transport.writes)
        # A successful upgrade leaves the handler task running; cancel it so the
        # loop closes without a "Task was destroyed but it is pending" warning.
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        return written
    finally:
        loop.close()


@pytest.mark.parametrize("path", ["/ws", "/nope"])
def test_the_built_in_server_refuses_a_disallowed_origin_before_matching(path):
    """The defect: an unknown path answered 404, telling a refused origin so."""
    assert b"403" in _native(path, _EVIL), path


def test_the_built_in_server_gives_a_refused_origin_one_answer_for_both_paths():
    assert _native("/ws", _EVIL) == _native("/nope", _EVIL)


def test_the_built_in_server_still_answers_404_for_an_allowed_unknown_path():
    """Gating first must not cost a legitimate client its 404."""
    assert b"404" in _native("/nope", _ALLOWED)


def test_the_built_in_server_still_upgrades_an_allowed_known_path():
    assert b"101" in _native("/ws", _ALLOWED)
