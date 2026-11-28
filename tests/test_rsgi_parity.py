"""RSGI parity — `RsgiMixin.__rsgi__` reaches the same dispatch ASGI does.

Driven against the fake scope/protocol pair in `tests/_rsgi_drive.py` (the
real objects are implemented in Rust with no public Python constructor).
`__rsgi__` translates into `__call__`, not `_asgi_app`/`_asgi_websocket`
directly, so these tests pin that the translation is faithful - not that
dispatch itself works, which the ASGI test suite already covers. Going
through `__call__` specifically (rather than its two internal helpers) is
what applies a registered raw-ASGI middleware wrap and matches an ASGI
mount for a WebSocket, not just for HTTP.
"""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Request, Response, StreamingResponse, Veloce
from veloce.app.rsgi import _split_host_port
from veloce.middleware import Middleware

from ._rsgi_drive import (
    FakeRsgiHeaders,
    FakeRsgiHttpProtocol,
    FakeRsgiScope,
    FakeRsgiWebsocketProtocol,
    ws_bytes,
    ws_close,
    ws_text,
)


def _http_scope(**overrides: object) -> FakeRsgiScope:
    defaults: dict[str, object] = {"proto": "http", "method": "GET", "path": "/"}
    defaults.update(overrides)
    return FakeRsgiScope(**defaults)  # type: ignore[arg-type]


async def _drive_http(app: Veloce, scope: FakeRsgiScope, protocol: FakeRsgiHttpProtocol) -> None:
    await app.__rsgi__(scope, protocol)


async def test_a_plain_json_route_responds_with_bytes():
    app = Veloce(openapi_url=None)

    @app.get("/hello")
    async def hello() -> dict:
        return {"message": "hi"}

    protocol = FakeRsgiHttpProtocol()
    await _drive_http(app, _http_scope(path="/hello"), protocol)

    assert protocol.status == 200
    assert protocol.body == b'{"message":"hi"}'
    assert any(k.lower() == "content-type" for k, _v in protocol.headers)


async def test_a_path_parameter_is_matched_and_coerced():
    app = Veloce(openapi_url=None)

    @app.get("/users/{user_id:int}")
    async def show(user_id: int) -> dict:
        return {"user_id": user_id}

    protocol = FakeRsgiHttpProtocol()
    await _drive_http(app, _http_scope(path="/users/42"), protocol)

    assert protocol.status == 200
    assert protocol.body == b'{"user_id":42}'


class _Item(BaseModel):
    name: str
    price: float


async def test_a_post_body_is_read_and_validated():
    app = Veloce(openapi_url=None)

    @app.post("/items")
    async def create(item: _Item) -> dict:
        return {"name": item.name, "price": item.price}

    body = b'{"name":"Widget","price":9.99}'
    protocol = FakeRsgiHttpProtocol(body=body)
    scope = _http_scope(
        method="POST",
        path="/items",
        headers=FakeRsgiHeaders([("content-type", "application/json")]),
    )
    await _drive_http(app, scope, protocol)

    assert protocol.status == 200
    assert protocol.body == b'{"name":"Widget","price":9.99}'


async def test_query_string_reaches_the_handler():
    app = Veloce(openapi_url=None)

    @app.get("/search")
    async def search(q: str) -> dict:
        return {"q": q}

    protocol = FakeRsgiHttpProtocol()
    await _drive_http(app, _http_scope(path="/search", query_string="q=veloce"), protocol)

    assert protocol.body == b'{"q":"veloce"}'


async def test_a_streaming_response_uses_the_rsgi_stream_transport():
    app = Veloce(openapi_url=None)

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks():
            yield b"a"
            yield b"b"
            yield b"c"

        return StreamingResponse(chunks(), content_type="text/plain")

    protocol = FakeRsgiHttpProtocol()
    await _drive_http(app, _http_scope(path="/stream"), protocol)

    assert protocol.stream is not None
    assert protocol.streamed_body == b"abc"


async def test_a_stream_true_route_consumes_the_rsgi_body_iterator():
    app = Veloce(openapi_url=None)

    @app.post("/upload", stream=True)
    async def upload(request: Request) -> dict:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return {"received": total}

    body = b"x" * 10_000
    protocol = FakeRsgiHttpProtocol(body=body, chunk_size=1000)
    scope = _http_scope(method="POST", path="/upload")
    await _drive_http(app, scope, protocol)

    assert protocol.body == b'{"received":10000}'


async def test_max_content_length_refuses_an_oversized_streamed_body():
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = 100

    @app.post("/upload", stream=True)
    async def upload(request: Request) -> dict:
        async for _chunk in request.stream():
            pass
        return {"ok": True}  # pragma: no cover - refused before this runs

    protocol = FakeRsgiHttpProtocol(body=b"x" * 1000, chunk_size=200)
    scope = _http_scope(method="POST", path="/upload")
    await _drive_http(app, scope, protocol)

    assert protocol.status == 413


async def test_a_mounted_asgi_app_is_reached_under_rsgi():
    async def tiny_asgi(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": f"path={scope['path']}".encode()})

    app = Veloce(openapi_url=None)
    app.mount("/ext", tiny_asgi)

    protocol = FakeRsgiHttpProtocol()
    await _drive_http(app, _http_scope(path="/ext/hello"), protocol)

    assert protocol.body == b"path=/hello"


async def test_registered_middleware_runs_under_rsgi():
    class AddHeader(Middleware):
        async def process_response(self, request: Request, response: Response) -> Response:
            response.headers["X-From-Middleware"] = "yes"
            return response

    app = Veloce(openapi_url=None)
    app.add_middleware(AddHeader)

    @app.get("/hello")
    async def hello() -> dict:
        return {"ok": True}

    protocol = FakeRsgiHttpProtocol()
    await _drive_http(app, _http_scope(path="/hello"), protocol)

    headers = {k.lower(): v for k, v in protocol.headers}
    assert headers["x-from-middleware"] == "yes"


async def test_a_raw_asgi_middleware_wrap_runs_under_rsgi():
    """Not a `veloce.Middleware` subclass - compiled into `cp.asgi_wrap` and
    applied only by `__call__`'s own stack-building branch, never by
    `_asgi_app` alone. Calling `_asgi_app` directly - the bug this pins -
    would skip it silently."""

    class HeaderMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            async def _send(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((b"x-asgi-wrap", b"yes"))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, _send)

    app = Veloce(openapi_url=None)
    app.add_middleware(HeaderMiddleware)

    @app.get("/hello")
    async def hello() -> dict:
        return {"ok": True}

    protocol = FakeRsgiHttpProtocol()
    await _drive_http(app, _http_scope(path="/hello"), protocol)

    headers = {k.lower(): v for k, v in protocol.headers}
    assert headers["x-asgi-wrap"] == "yes"


# ── WebSocket ───────────────────────────────────────────────


async def test_a_websocket_handler_is_accepted_and_echoes():
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def echo(ws):
        async with ws:
            await ws.accept()
            text = await ws.receive_text()
            await ws.send_text(f"echo:{text}")

    ws_protocol = FakeRsgiWebsocketProtocol(inbound=[ws_text("hi"), ws_close()])
    scope = FakeRsgiScope(proto="ws", path="/ws")
    await app.__rsgi__(scope, ws_protocol)

    assert ws_protocol.transport is not None
    assert ws_protocol.transport.sent == [("str", "echo:hi")]


async def test_a_websocket_handler_sees_binary_messages():
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def echo(ws):
        async with ws:
            await ws.accept()
            data = await ws.receive_bytes()
            await ws.send_bytes(data[::-1])

    ws_protocol = FakeRsgiWebsocketProtocol(inbound=[ws_bytes(b"abc"), ws_close()])
    await app.__rsgi__(FakeRsgiScope(proto="ws", path="/ws"), ws_protocol)

    assert ws_protocol.transport.sent == [("bytes", b"cba")]


async def test_an_unmatched_websocket_route_is_refused_without_accepting():
    app = Veloce(openapi_url=None)

    ws_protocol = FakeRsgiWebsocketProtocol(inbound=[])
    await app.__rsgi__(FakeRsgiScope(proto="ws", path="/no-such-route"), ws_protocol)

    assert ws_protocol.transport is None
    assert ws_protocol.close_status is not None


async def test_a_mounted_asgi_websocket_app_is_reached_under_rsgi():
    """The ASGI mount match lives in `_asgi_app`'s own `scope["type"] in
    (http, websocket)` branch, reached only through `__call__`/`_asgi_app` -
    `_asgi_websocket` alone has no mount-matching of its own, so calling it
    directly for every `ws` scope (the bug this pins) would never delegate
    to a mounted sub-app's WebSocket route at all."""

    async def tiny_asgi(scope, receive, send):
        connect = await receive()
        assert connect["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        message = await receive()
        await send({"type": "websocket.send", "text": f"mounted:{message['text']}"})

    app = Veloce(openapi_url=None)
    app.mount("/ext", tiny_asgi)

    ws_protocol = FakeRsgiWebsocketProtocol(inbound=[ws_text("hi")])
    await app.__rsgi__(FakeRsgiScope(proto="ws", path="/ext/ws"), ws_protocol)

    assert ws_protocol.transport is not None
    assert ws_protocol.transport.sent == [("str", "mounted:hi")]


async def test_a_close_reason_does_not_reach_the_rsgi_protocol():
    """RSGI's `close()` takes no reason string; only the code carries over."""
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws):
        async with ws:
            await ws.accept()
            await ws.close(code=4001, reason="bye")

    ws_protocol = FakeRsgiWebsocketProtocol(inbound=[])
    await app.__rsgi__(FakeRsgiScope(proto="ws", path="/ws"), ws_protocol)

    assert ws_protocol.close_status == 4001


# ── host:port splitting ──────────────────────────────────────


def test_split_host_port_strips_ipv6_brackets():
    assert _split_host_port("[::1]:8080") == ("::1", 8080)


def test_split_host_port_handles_ipv4():
    assert _split_host_port("127.0.0.1:8000") == ("127.0.0.1", 8000)


def test_split_host_port_handles_a_bracketed_full_ipv6_address():
    assert _split_host_port("[2001:db8::1]:443") == ("2001:db8::1", 443)


def test_split_host_port_does_not_guess_on_a_bare_unbracketed_ipv6_host():
    """No bracket convention is documented for this case - keeping the whole
    value as the host (port lost) is safer than guessing the wrong split."""
    assert _split_host_port("2001:db8::1") == ("2001:db8::1", 0)


async def test_a_bracketed_ipv6_client_address_reaches_the_request_unbracketed():
    app = Veloce(openapi_url=None)

    @app.get("/addr")
    async def addr(request: Request) -> dict:
        return {"client": list(request.scope["client"])}

    protocol = FakeRsgiHttpProtocol()
    scope = _http_scope(path="/addr", client="[::1]:51234")
    await _drive_http(app, scope, protocol)

    assert protocol.body == b'{"client":["::1",51234]}'
