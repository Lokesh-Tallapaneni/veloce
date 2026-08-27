"""A native-transport 413 carries the same headers as the ASGI one.

`app/asgi.py` routes its `413` through `_body_too_large_response`, which runs the
app's response phase, and the comment beside it records why: "a cross-origin
upload that tripped the limit reached the client as an opaque CORS failure". The
native `HttpProtocol` hand-framed its own refusal with nothing but a
`Content-Type`, so `app.run()` and `VeloceWorker` served a different refusal
surface than uvicorn for the same app and the same configuration.

The declared-`Content-Length` refusal now runs the response phase. That is the
one refusal where a full request line and headers exist and nothing has been
dispatched yet, which is what makes running middleware against it both possible
and safe.

**The refusals that are still hand-framed, and why.** These are not oversights:

| refusal | why the response phase cannot run |
|---|---|
| `400` | the bytes did not parse - there is no request to run middleware against |
| `414`/`431` | the request line or headers are themselves what exceeded the cap |
| `503` admission | fires at `connection_made`, before any bytes are read |
| `408` read timeout | may fire mid-headers, so a request may not exist |
| pre-`101` WS refusals | the handshake failed; no HTTP response phase applies |
| streamed-body `413` | the request was dispatched at headers-complete and the handler owns the response; a second one on the same request would be a protocol error |

Each of those has no request, or has one that something else already owns.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce import CORSMiddleware, Middleware, SecurityHeadersMiddleware, Veloce
from veloce.serving.protocol import HttpProtocol

_ORIGIN = "https://ok.example"


class _Transport(asyncio.Transport):
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


def _app(**middleware) -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = 100
    if middleware.get("cors", True):
        app.add_middleware(CORSMiddleware(allow_origins=[_ORIGIN]))
    if middleware.get("security", False):
        app.add_middleware(SecurityHeadersMiddleware())

    @app.post("/up")
    async def up(request):
        return {"ok": True}

    return app


def _native(app, declared: int, origin: str | None = _ORIGIN) -> dict[str, str]:
    """Drive one over-limit POST through `HttpProtocol`; return its headers."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(app, loop)
        transport = _Transport()
        proto.connection_made(transport)
        head = b"POST /up HTTP/1.1\r\nHost: t\r\n"
        if origin is not None:
            head += b"Origin: " + origin.encode() + b"\r\n"
        head += b"Content-Length: " + str(declared).encode() + b"\r\n\r\n"
        proto.data_received(head)
        for _ in range(8):
            loop.run_until_complete(asyncio.sleep(0))
        emitted = b"".join(transport.writes)
    finally:
        loop.close()
    block = emitted.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = block.split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    headers["__status__"] = lines[0].split()[1]
    headers["__body__"] = emitted.split(b"\r\n\r\n", 1)[1].decode("latin-1")
    return headers


def _asgi(app, declared: int, origin: str | None = _ORIGIN) -> dict[str, str]:
    """Drive the same request through the ASGI entry point."""
    headers = [(b"host", b"t"), (b"content-length", str(declared).encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    captured: dict[str, str] = {}
    body_parts: list[bytes] = []

    async def run():
        sent = [False]

        async def receive():
            if sent[0]:
                return {"type": "http.disconnect"}
            sent[0] = True
            return {"type": "http.request", "body": b"x" * declared, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                captured["__status__"] = str(message["status"])
                for k, v in message["headers"]:
                    captured[k.decode().lower()] = v.decode()
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))

        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/up",
                "raw_path": b"/up",
                "query_string": b"",
                "headers": headers,
                "client": ("1.2.3.4", 1),
                "scheme": "http",
                "server": ("t", 80),
                "http_version": "1.1",
                "root_path": "",
            },
            receive,
            send,
        )

    asyncio.run(run())
    captured["__body__"] = b"".join(body_parts).decode("latin-1")
    return captured


# ── the two transports agree ─────────────────────────────────────────


def test_the_native_413_carries_the_cors_vary_header():
    """The defect: the native refusal had a `Content-Type` and nothing else."""
    assert _native(_app(), 500).get("vary") == "Origin"


def test_both_transports_agree_on_vary():
    """Asserted against each other, not against a fixed list."""
    assert _native(_app(), 500).get("vary") == _asgi(_app(), 500).get("vary")


def test_both_transports_agree_on_status():
    assert _native(_app(), 500)["__status__"] == _asgi(_app(), 500)["__status__"]


def test_both_transports_agree_on_the_body():
    assert _native(_app(), 500)["__body__"] == _asgi(_app(), 500)["__body__"]


def test_both_transports_agree_on_content_type():
    assert _native(_app(), 500)["content-type"] == _asgi(_app(), 500)["content-type"]


def test_security_headers_reach_the_native_refusal():
    """Any response middleware, not just CORS."""
    headers = _native(_app(security=True), 500)
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("x-frame-options") == "DENY"


def test_both_transports_agree_on_the_security_headers():
    native = _native(_app(security=True), 500)
    asgi = _asgi(_app(security=True), 500)
    for header in ("x-content-type-options", "x-frame-options", "referrer-policy"):
        assert native.get(header) == asgi.get(header), header


def test_a_request_with_no_origin_still_gets_the_refusal():
    headers = _native(_app(), 500, origin=None)
    assert headers["__status__"] == "413"


# ── the refusal is still a refusal ───────────────────────────────────
#
# The negatives. Running middleware must not soften the rejection, keep the
# connection alive, or let the over-limit body through.


def test_the_refusal_still_closes_the_connection():
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(_app(), loop)
        transport = _Transport()
        proto.connection_made(transport)
        proto.data_received(b"POST /up HTTP/1.1\r\nHost: t\r\nContent-Length: 500\r\n\r\n")
        for _ in range(8):
            loop.run_until_complete(asyncio.sleep(0))
        assert transport.closed
    finally:
        loop.close()


def test_the_refusal_says_connection_close():
    assert _native(_app(), 500).get("connection") == "close"


def test_the_handler_never_runs():
    calls = []
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = 100
    app.add_middleware(CORSMiddleware(allow_origins=[_ORIGIN]))

    @app.post("/up")
    async def up(request):
        calls.append(1)
        return {"ok": True}

    _native(app, 500)
    assert calls == []


def test_a_body_within_the_limit_is_served_normally():
    """The refusal path must not capture an honest request."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(_app(), loop)
        transport = _Transport()
        proto.connection_made(transport)
        proto.data_received(
            b"POST /up HTTP/1.1\r\nHost: t\r\nContent-Length: 4\r\n\r\nxxxx",
        )
        for _ in range(40):
            loop.run_until_complete(asyncio.sleep(0))
        assert b"HTTP/1.1 200" in b"".join(transport.writes)
    finally:
        loop.close()


def test_an_app_with_no_response_middleware_still_refuses():
    """No middleware chain means the bare framing, which must still be correct."""
    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = 100

    @app.post("/up")
    async def up(request):
        return {"ok": True}

    headers = _native(app, 500)
    assert headers["__status__"] == "413"
    assert headers["content-type"].startswith("application/json")


def test_middleware_that_raises_does_not_swallow_the_refusal():
    """A refusal must reach the client even if the response phase fails."""

    class Broken(Middleware):
        async def process_response(self, request, response):
            raise RuntimeError("boom")

    app = Veloce(openapi_url=None)
    app.config["MAX_CONTENT_LENGTH"] = 100
    app.add_middleware(Broken())

    @app.post("/up")
    async def up(request):
        return {"ok": True}

    assert _native(app, 500)["__status__"] == "413"


# ── the refusals that stay hand-framed ───────────────────────────────
#
# Pinned so a later change does not quietly reroute one of them through a
# response phase it has no request for.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"NOT-HTTP\r\n\r\n", "400"),
        (b"GET /" + b"a" * 100_000 + b" HTTP/1.1\r\nHost: t\r\n\r\n", "414"),
    ],
    ids=["unparseable", "oversized-request-line"],
)
def test_an_unparseable_request_is_still_refused_without_middleware(raw, expected):
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(_app(), loop)
        transport = _Transport()
        proto.connection_made(transport)
        proto.data_received(raw)
        for _ in range(8):
            loop.run_until_complete(asyncio.sleep(0))
        emitted = b"".join(transport.writes)
    finally:
        loop.close()
    assert emitted.startswith(b"HTTP/1.1 " + expected.encode())
    assert b"Vary" not in emitted


def test_a_streamed_over_limit_body_is_still_refused():
    """No declared `Content-Length`, so the running total is the backstop."""
    app = _app()
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(app, loop)
        transport = _Transport()
        proto.connection_made(transport)
        proto.data_received(b"POST /up HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n")
        for _ in range(4):
            loop.run_until_complete(asyncio.sleep(0))
        chunk = b"ff\r\n" + b"x" * 255 + b"\r\n"
        for _ in range(4):
            proto.data_received(chunk)
            for _ in range(2):
                loop.run_until_complete(asyncio.sleep(0))
        emitted = b"".join(transport.writes)
    finally:
        loop.close()
    assert b"413" in emitted
