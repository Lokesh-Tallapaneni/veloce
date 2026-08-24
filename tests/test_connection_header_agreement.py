"""The `Connection` header states what the transport actually decided.

Whether a connection survives a response is decided by the protocol - from the
request's HTTP version and its own `Connection` header - and acted on by
closing the socket or serving the next request. Every response head, meanwhile,
hardcoded `Connection: keep-alive`.

So an HTTP/1.0 request, or one asking for `Connection: close`, was answered by a
server that closed the socket and a header saying it had not. An intermediary or
client that trusts the header reuses a socket that is already gone. A native SSE
stream was worse: that path closes the connection when the generator ends, and
`EventSourceResponse` wrote `keep-alive` into its own headers, where it outranked
the transport entirely.

`_encode_response_head` now takes the decision as a *required* keyword, so a
response type added later cannot inherit a default that contradicts its
transport.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce import Response, Veloce
from veloce._internal import _encode_response_head
from veloce.http.response import StreamingResponse
from veloce.serving.protocol import HttpProtocol
from veloce.sse import EventSourceResponse, ServerSentEvent


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


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/hello")
    async def hello():
        return {"ok": True}

    return app


def _serve(raw: bytes) -> tuple[bytes, bool]:
    """Drive one raw request through the native protocol."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(_app(), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)
        proto.data_received(raw)
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))
        return b"".join(transport.writes), transport.closed
    finally:
        loop.close()


# ── The head agrees with what the socket does ────────────────────────


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("HTTP/1.0", b"GET /hello HTTP/1.0\r\nHost: x\r\n\r\n"),
        ("Connection: close", b"GET /hello HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"),
    ],
)
def test_a_connection_the_server_closes_is_not_advertised_as_reusable(label, raw):
    """The defect: the server closed the socket and the header said keep-alive."""
    emitted, closed = _serve(raw)
    assert closed is True, label
    assert b"Connection: keep-alive" not in emitted, label
    assert b"Connection: close" in emitted, label


def test_a_kept_connection_is_still_advertised_as_reusable():
    emitted, closed = _serve(b"GET /hello HTTP/1.1\r\nHost: x\r\n\r\n")
    assert closed is False
    assert b"Connection: keep-alive" in emitted


# ── The decision reaches every response family ───────────────────────


async def _chunks():
    yield b"chunk"


@pytest.mark.parametrize(
    "build",
    [
        lambda: Response(body=b"hi", content_type="text/plain"),
        lambda: StreamingResponse(_chunks(), content_type="text/plain"),
        lambda: EventSourceResponse(_chunks()),
    ],
    ids=["buffered", "streamed", "event-source"],
)
@pytest.mark.parametrize("keep_alive", [True, False])
def test_every_response_family_advertises_the_decision(build, keep_alive):
    head = build().encode(keep_alive=keep_alive)
    expected = b"Connection: keep-alive" if keep_alive else b"Connection: close"
    unexpected = b"Connection: close" if keep_alive else b"Connection: keep-alive"
    assert expected in head
    assert unexpected not in head


def test_an_event_source_no_longer_carries_connection_as_its_own_header():
    """It outranked the transport, which is what made the SSE case unfixable."""
    response = EventSourceResponse(_chunks())
    assert "Connection" not in response.headers


def test_a_native_sse_stream_advertises_close_because_the_path_closes():
    app = Veloce(openapi_url=None)

    @app.get("/sse")
    async def sse():
        async def events():
            yield ServerSentEvent.json({"a": 1})

        return EventSourceResponse(events())

    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)
        proto.data_received(b"GET /sse HTTP/1.1\r\nHost: x\r\n\r\n")
        for _ in range(6):
            loop.run_until_complete(asyncio.sleep(0))
        emitted = b"".join(transport.writes)
    finally:
        loop.close()

    assert b"Connection: close" in emitted
    assert b"Connection: keep-alive" not in emitted


# ── The formatter refuses to guess ───────────────────────────────────


def test_the_head_builder_requires_the_decision():
    """A response type added later cannot inherit a contradicting default."""
    with pytest.raises(TypeError):
        _encode_response_head(200, {}, {})


def test_a_caller_set_connection_header_still_wins():
    """An explicit header remains the caller's to state."""
    response = Response(body=b"hi", headers={"Connection": "close"})
    assert b"Connection: close" in response.encode(keep_alive=True)


# ── The encode cache cannot serve the wrong decision ─────────────────


def test_the_cached_encode_is_not_reused_for_a_closing_connection():
    """The cache holds the keep-alive head; a close must not get it."""
    response = Response(body=b"hi", content_type="text/plain")
    assert b"Connection: keep-alive" in response.encode()
    assert b"Connection: close" in response.encode(keep_alive=False)
    # ...and the cached keep-alive head survives for the next reuse.
    assert b"Connection: keep-alive" in response.encode()
