"""ASGI plumbing for the test client — scopes, the receive channel, response collection."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
import sys
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from veloce._protocol_constants import (
    ASGI_EVENT_HTTP_DISCONNECT,
    ASGI_EVENT_HTTP_REQUEST,
    ASGI_EVENT_HTTP_RESPONSE_BODY,
    ASGI_EVENT_HTTP_RESPONSE_START,
    ASGI_SCOPE_HTTP,
    URL_SCHEME_HTTP,
)
from veloce.status import (
    HTTP_500_INTERNAL_SERVER_ERROR,
)

if TYPE_CHECKING:  # pragma: no cover
    pass
from veloce.testclient._body import StreamBody, _aiter_body_chunks
from veloce.testclient.response import TestResponse

# Turns a read past end-of-body yields before it is told the client is gone.
# `poll_disconnected` cancels after one, so answering inside it would tell a
# streaming handler its client had gone; a real read survives and is answered.
_PEEK_TURNS = 3


class _ClientChannel:
    """The ASGI `receive` channel both test clients hand the app.

    With no `stream` a single `http.request` frame carries the whole body; with
    one, each chunk becomes a `more_body: True` frame before a terminal empty one.

    A read past end-of-body is answered `http.disconnect`, since this client has
    no socket and nothing else can arrive - but not within the turn a *peek*
    lasts. `request.is_disconnected()` cannot tell "no more input" from "the peer
    is gone", so yielding `_PEEK_TURNS` first separates the two callers by what
    they do rather than what they ask. Once the exchange is finished the answer is
    immediate, which is where a server does report the disconnect.
    """

    __slots__ = ("_body", "_chunks", "_body_sent", "_exhausted", "_finished")

    def __init__(self, body: bytes, stream: StreamBody | None) -> None:
        self._body = body
        self._chunks = None if stream is None else _aiter_body_chunks(stream)
        self._body_sent = False
        self._exhausted = False
        self._finished = asyncio.Event()

    def exchange_finished(self) -> None:
        """Record that the response is complete, so a later read answers at once."""
        self._finished.set()

    async def __call__(self) -> dict[str, Any]:
        chunks = self._chunks
        if chunks is None:
            if not self._body_sent:
                self._body_sent = True
                return {"type": ASGI_EVENT_HTTP_REQUEST, "body": self._body, "more_body": False}
        elif not self._exhausted:
            try:
                chunk = await chunks.__anext__()
            except StopAsyncIteration:
                self._exhausted = True
                return {"type": ASGI_EVENT_HTTP_REQUEST, "body": b"", "more_body": False}
            return {"type": ASGI_EVENT_HTTP_REQUEST, "body": chunk, "more_body": True}
        for _ in range(_PEEK_TURNS):
            if self._finished.is_set():
                break
            await asyncio.sleep(0)
        return {"type": ASGI_EVENT_HTTP_DISCONNECT}


async def _send_one_request(
    app: Any,
    method: str,
    path: str,
    query_string: str,
    headers: list[tuple[str, str]],
    body: bytes,
    stream: StreamBody | None = None,
) -> TestResponse:
    """Drive one ASGI request through `app` and collect its response.

    Shared by both clients so a change to the scope or receive channel cannot
    reach one and miss the other.
    """
    scope = _build_scope(method, path, query_string, headers)
    return await _collect_response(app, scope, _ClientChannel(body, stream))


def _build_scope(
    method: str,
    path: str,
    query_string: str,
    headers: list[tuple[str, str]],
    client: tuple[str, int] = ("testclient", 50000),
    server: tuple[str, int] = ("testserver", 80),
    scheme: str = URL_SCHEME_HTTP,
    root_path: str = "",
) -> dict[str, Any]:
    """Build an ASGI 3.0 HTTP scope. Header values are encoded latin-1 per spec."""
    raw_headers: list[tuple[bytes, bytes]] = []
    seen_host = False
    for k, v in headers:
        if k.lower() == "host":
            seen_host = True
        raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    if not seen_host:
        raw_headers.append((b"host", server[0].encode("latin-1")))

    return {
        "type": ASGI_SCOPE_HTTP,
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": scheme,
        # ASGI defines `path` percent-decoded, as a real server hands it to the app,
        # or a decoding-dependent bug is invisible to the in-process suite.
        "path": unquote(path),
        # ASGI permits non-ASCII bytes in `raw_path`, so `encode('ascii')` would
        # crash on a non-ASCII URL. This stays undecoded - it is the raw target.
        "raw_path": path.encode("utf-8"),
        "query_string": query_string.encode("ascii"),
        "root_path": root_path,
        "headers": raw_headers,
        "client": client,
        "server": server,
    }


async def _collect_response(
    app: Any, scope: dict[str, Any], receive: _ClientChannel
) -> TestResponse:
    """Drive one ASGI request/response cycle and assemble a `TestResponse`.

    Shared by the sync and async clients' `_send_one_request` so the `send`
    closure and body assembly live in one place (a fix to either applies to
    both, per the async/sync parity guardrail).
    """
    status_code = HTTP_500_INTERNAL_SERVER_ERROR
    raw_headers: list[tuple[bytes, bytes]] = []
    body_chunks: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code, raw_headers
        mtype = message["type"]
        if mtype == ASGI_EVENT_HTTP_RESPONSE_START:
            status_code = message["status"]
            raw_headers = list(message.get("headers") or [])
        elif mtype == ASGI_EVENT_HTTP_RESPONSE_BODY:
            chunk = message.get("body", b"")
            if chunk:
                body_chunks.append(chunk)
            if not message.get("more_body", False):
                # End of response: where a server starts reporting `http.disconnect`, so
                # from here a read past end-of-body is answered without yielding.
                receive.exchange_finished()

    try:
        await app(scope, receive, send)
    finally:
        # An app that raised, or ended without a terminal body message, never
        # reached the branch above; the exchange is over either way.
        receive.exchange_finished()
    return TestResponse(status_code, b"".join(body_chunks), raw_headers)


#: `SO_LINGER` with `onoff=1, linger=0` - close sends RST, so the socket skips
#: `TIME_WAIT`. Windows lays `linger` out as two `u_short`, POSIX as two `int`.
_ABORTIVE_CLOSE = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)


def _abort_self_pipe_on_close(loop: asyncio.AbstractEventLoop) -> None:
    """Make the selector loop's wakeup pipe release its port on close.

    The pipe is a loopback TCP socketpair, and a normal close leaves one end in
    `TIME_WAIT` for minutes - one ephemeral port per client, against a dynamic
    range only 16384 wide. It carries wakeup bytes nothing reads at shutdown, so
    an abortive close loses nothing.

    Only the loops `_new_loop` builds, so this slows the rate at which the range
    is consumed rather than removing the failure. `_ssock` / `_csock` are CPython
    internals, so a rename gets today's behaviour back rather than an exception.
    """
    for name in ("_ssock", "_csock"):
        sock = getattr(loop, name, None)
        if sock is not None:
            # Best effort: a family refusing `SO_LINGER` must not fail every test.
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _ABORTIVE_CLOSE)


def _new_loop() -> asyncio.AbstractEventLoop:
    """Build the event loop a test client drives its app on.

    On Windows the default proactor loop runs an I/O completion port per
    iteration, and the in-memory client opens no socket for it to find: the
    selector loop measured 15.5% more requests per second here. It costs a
    socketpair, whose port `_abort_self_pipe_on_close` hands straight back.

    It cannot run `asyncio.create_subprocess_*`, so a handler that spawns one
    takes `TestClient(app, loop=asyncio.ProactorEventLoop())` instead.
    """
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop()
        _abort_self_pipe_on_close(loop)
        return loop
    return asyncio.new_event_loop()
