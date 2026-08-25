"""Serving protocol — high-performance HTTP/1.1 over a raw asyncio transport.

Parses the wire with httptools and dispatches requests through the Veloce app,
bypassing the ASGI layer entirely.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import socket as _socket
import threading
import weakref
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from urllib.parse import unquote

import httptools
import orjson

from veloce import status
from veloce._constants import (
    MIME_TEXT_PLAIN,
    MSG_ERROR_RESPONSE_EMISSION,
    MSG_INTERNAL_SERVER_ERROR,
)
from veloce._internal import _extract_host, _ws_handshake_rejection
from veloce._protocol_constants import (
    HTTP_METHOD_HEAD,
    RAW_HEADER_CONTENT_LENGTH,
    ROUTE_METHOD_WEBSOCKET,
)
from veloce.config import (
    DEFAULT_MAX_CONCURRENT_CONNECTIONS,
    DEFAULT_WRITE_BUFFER_HIGH_WATER,
)
from veloce.config import Config as _Config
from veloce.exceptions import RequestEntityTooLarge, WebSocketDisconnect
from veloce.http._body import RequestBodySource, too_large_payload
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.websocket import WebSocket, compute_accept

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce
    from veloce.routing.router import RouteMatch

_logger = logging.getLogger(__name__)


# Per-field + cumulative caps on the request line and headers. Prevents a
# malicious client from streaming megabytes of headers and pinning RAM
# before the body limit (MAX_CONTENT_LENGTH) gets a chance to engage.
MAX_URL_SIZE = 8192
MAX_HEADER_SIZE = 8192
MAX_TOTAL_HEADERS_SIZE = 65536

# Both defaults live with the other config defaults so `default_config()` lists
# every key this path reads; re-exported here under the names this module has
# always used.
WRITE_BUFFER_HIGH_WATER = DEFAULT_WRITE_BUFFER_HIGH_WATER

# Process-wide graceful-shutdown latch. Phase one of `Veloce._graceful_shutdown`
# sets this and flips every live connection's `_draining` flag; a connection
# admitted in the race window after the latch is set reads it in
# `connection_made` and starts draining at once, so it serves at most one
# request before quiescing. Reset is unnecessary - shutdown is terminal for the
# process - but the helper that clears it exists for the test suite, which
# drives shutdown repeatedly within one interpreter.
_SHUTTING_DOWN = False


def _enable_tcp_keepalive(
    sock: _socket.socket,
    idle: int | None,
    interval: int | None,
    count: int | None,
) -> None:
    """Turn on OS-level TCP keepalive for an accepted connection socket.

    Sets SO_KEEPALIVE so the kernel probes an idle peer and reaps a half-open
    connection that vanished without a FIN - a dead peer the application-level
    idle timer never observes because no bytes ever arrive. The per-socket
    tuning options are platform-specific: TCP_KEEPIDLE / TCP_KEEPINTVL /
    TCP_KEEPCNT exist on Linux (and TCP_KEEPALIVE names the idle option on
    macOS), but Windows exposes none of them via setsockopt. Each option is
    therefore probed with `getattr` against the `socket` module and applied only
    when both the constant and a value are present, so the native serving path
    keeps working on Windows where only SO_KEEPALIVE itself is available. A
    `None` value leaves the OS default in place. Failures to set any individual
    option are swallowed - keepalive is best-effort hardening, never a reason to
    drop an otherwise-serviceable connection.
    """
    with contextlib.suppress(OSError):
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
    # `TCP_KEEPIDLE` is the Linux name; macOS spells the idle option
    # `TCP_KEEPALIVE`. Prefer whichever this build exposes.
    idle_opt = getattr(_socket, "TCP_KEEPIDLE", None)
    if idle_opt is None:
        idle_opt = getattr(_socket, "TCP_KEEPALIVE", None)
    for value, opt in (
        (idle, idle_opt),
        (interval, getattr(_socket, "TCP_KEEPINTVL", None)),
        (count, getattr(_socket, "TCP_KEEPCNT", None)),
    ):
        if value is None or opt is None:
            continue
        with contextlib.suppress(OSError):
            sock.setsockopt(_socket.IPPROTO_TCP, opt, value)


def _strip_response_body(encoded: bytes) -> bytes:
    """Return the header section of an encoded response, dropping the body.

    `Response.encode()` emits the header block, a blank-line terminator, then
    the body. A HEAD response keeps the header section (with the would-be
    Content-Length) but sends no body (RFC 9110 Sec. 9.3.2). The header section
    ends at the first CRLFCRLF; that separator is always present.
    """
    return encoded[: encoded.index(b"\r\n\r\n") + 4]


# RFC 6455 Sec. 4.2.1: a WebSocket handshake is a GET carrying the upgrade
# triplet. These are the lowercased header names the upgrade detector scans for
# (httptools delivers names already lowercased via `on_header`).
_WS_HEADER_CONNECTION = b"connection"
_WS_HEADER_UPGRADE = b"upgrade"
_WS_HEADER_KEY = b"sec-websocket-key"
_WS_HEADER_VERSION = b"sec-websocket-version"
_WS_HEADER_HOST = b"host"
_WS_HEADER_ORIGIN = b"origin"
# RFC 6455 Sec. 4.2.2: the only WebSocket protocol version Veloce speaks.
_WS_SUPPORTED_VERSION = b"13"


#: `%` as an int, for the percent-escape test on the request target. Membership
#: of an int in `bytes` is a memchr; membership of a one-byte `bytes` is a
#: substring search, an order of magnitude dearer on a path this runs on for
#: every request.
_PERCENT_BYTE = 0x25


class HttpProtocol(asyncio.Protocol):
    """Raw asyncio protocol - bypasses ASGI overhead entirely."""

    __slots__ = (
        "app",
        "loop",
        "transport",
        "parser",
        "url",
        "headers",
        "_keep_alive_handle",
        "_request_timer",
        "_header_bytes_total",
        "_headers_done",
        "_oversized",
        "_counted",
        "_request_queue",
        "_server_loop",
        "_closing",
        "_draining",
        "_current_source",
        "_raw_content_length",
        "_has_expect_continue",
        "_can_write",
        "_websocket",
        "_ws_task",
        # Allow weak references so live connections can be tracked in a
        # `WeakSet` for graceful-shutdown draining without pinning the object.
        "__weakref__",
    )

    # Class-level set: prevents GC of in-flight tasks across all connections.
    _active_tasks: set[asyncio.Task] = set()

    # Live, admitted connections, tracked so graceful shutdown can quiesce each
    # one at its next request boundary. A `WeakSet` so a connection that tears
    # down normally drops out without manual bookkeeping and never pins a closed
    # protocol object. Phase one of shutdown walks this once to flip every
    # connection's `_draining` flag; each `_serve` loop then self-quiesces
    # lazily, finishing its in-flight request and declining further pipelined
    # work rather than being cancelled mid-flight.
    _live_connections: weakref.WeakSet[HttpProtocol] = weakref.WeakSet()

    # Optional class-level hook invoked once per dispatched request, after the
    # response has been written. `None` by default so the per-request path pays
    # only a single `is not None` check. The gunicorn worker installs a callback
    # here to drive `max_requests` recycling; nothing else uses it. Set on the
    # class (process-wide) rather than per instance to avoid bloating every
    # connection object with a slot that is unused under uvicorn / Veloce.run().
    on_request_complete: Callable[[], None] | None = None

    # Optional class-level predicate consulted after each dispatched request to
    # decide whether the connection may serve the next queued/pipelined request.
    # `None` by default (always keep serving) so the uvicorn / Veloce.run() path
    # pays only an `is not None` check. The gunicorn worker installs a callback
    # returning `self.alive`, so once `max_requests` recycling clears `alive`
    # the per-connection loop stops at the request boundary instead of draining
    # the whole pipelined queue past the limit.
    should_keep_serving: Callable[[], bool] | None = None

    # Cross-thread connection counter. `+=` on a class-level int is read-
    # modify-write, not atomic under the GIL, so guard with a Lock. Only
    # contended on connection setup/teardown - not on the per-request path.
    _active_connections: int = 0
    _connections_lock: threading.Lock = threading.Lock()

    # Fallback for a protocol built without an app config. The shipped
    # default lives in `Config.default_config()`; declaring the number in
    # both places left two lines to keep in step for one value.
    KEEP_ALIVE_TIMEOUT = _Config.default_config()["KEEP_ALIVE_TIMEOUT"]
    # Slowloris guard: once a request's bytes start arriving, the whole
    # request line + headers + body must complete within this budget,
    # otherwise the connection is dropped with 408. Bounds how long a
    # deliberately slow client can pin a connection open.
    REQUEST_TIMEOUT = 30  # seconds

    def __init__(self, app: Veloce, loop: asyncio.AbstractEventLoop) -> None:
        self.app = app
        self.loop = loop
        self.transport: asyncio.Transport | None = None
        self.parser = httptools.HttpRequestParser(self)
        self.url: bytes = b""
        self.headers: list[tuple[bytes, bytes]] = []
        self._keep_alive_handle: asyncio.TimerHandle | None = None
        self._request_timer: asyncio.TimerHandle | None = None
        self._header_bytes_total: int = 0
        # True between headers-complete and message-complete. Chunked trailer
        # fields (RFC 9112 section 7.1.2) arrive through the same `on_header`
        # callback as ordinary headers; this flag marks them as belonging to
        # the in-flight message so they are never appended to the cleared
        # header buffer that the *next* pipelined request will fill.
        self._headers_done: bool = False
        # Once a header/URL cap trips we reject the connection but httptools
        # may keep delivering buffered callbacks; this flag short-circuits
        # them so we don't double-emit an error response.
        self._oversized: bool = False
        # Tracks whether this protocol incremented _active_connections so
        # connection_lost only decrements connections that were counted
        # (a connection refused at the cap never bumps the counter).
        self._counted: bool = False
        # HTTP/1.1 mandates FIFO response ordering on a connection. A Request
        # is built and enqueued the moment its headers finish parsing (before
        # its body arrives); a single per-connection server-loop task drains
        # the queue one at a time so a pipelined follow-up can never have its
        # response written first. The tuple carries the Request, its body
        # source (the protocol feeds body chunks into it), and the keep-alive
        # flag snapshotted at headers-complete.
        self._request_queue: deque[tuple[Request, RequestBodySource, bool, RouteMatch | None]] = (
            deque()
        )
        self._server_loop: asyncio.Task | None = None
        # Set on teardown so an in-flight server loop stops pulling more work
        # and a client that closes mid-pipeline does not wedge the loop.
        self._closing: bool = False
        # Set by `begin_drain()` during graceful shutdown. Distinct from
        # `_closing`: a draining connection finishes the request it is already
        # dispatching, then closes at the boundary instead of being cancelled,
        # whereas `_closing` tears the loop down immediately. Pipelined requests
        # queued behind the in-flight one are declined (the client retries on a
        # fresh connection), so no accepted-and-running request is cut off.
        self._draining: bool = False
        # The body source of the request currently being parsed off the wire.
        # `on_body` feeds it; `on_message_complete` signals EOF. Distinct from
        # the request the server loop is dispatching (an earlier one may still
        # be in flight while a pipelined follow-up's body streams in).
        self._current_source: RequestBodySource | None = None
        # Captured during header parsing so headers-complete reads them in O(1)
        # rather than rescanning the whole header list. The raw Content-Length
        # value (still bytes, parsed lazily) drives the early 413; the expect
        # flag drives the 100-continue interim. Both are cleared alongside the
        # header buffers in on_headers_complete so a pipelined follow-up starts
        # from a clean slate.
        self._raw_content_length: bytes | None = None
        self._has_expect_continue: bool = False
        # Write-side backpressure gate. Set (writable) by default; cleared by
        # `pause_writing` when the transport buffer crosses the high-water mark
        # and re-set by `resume_writing` once it drains below the low mark. The
        # streaming/SSE path and the native WebSocket send path both await
        # `drain()`, which only blocks while cleared, so the common keep-alive
        # path pays a single already-set `Event` check.
        self._can_write: asyncio.Event = asyncio.Event()
        self._can_write.set()
        # Native WebSocket mode. `None` on the HTTP fast path (a single
        # `is not None` check per `data_received`). Once a valid RFC 6455
        # upgrade is matched and the 101 is sent, the connection is diverted:
        # `_websocket` holds the raw-transport `WebSocket` and subsequent socket
        # bytes are fed to its frame parser instead of the HTTP parser. `_ws_task`
        # is the handler dispatch task, cancelled on connection_lost.
        self._websocket: WebSocket | None = None
        self._ws_task: asyncio.Task | None = None

    # ── httptools callbacks ───────────────────────────────

    def on_url(self, url: bytes) -> None:
        if self._oversized:
            return
        if len(url) > MAX_URL_SIZE:
            self._reject_oversized(status.HTTP_414_REQUEST_URI_TOO_LONG, b"URI Too Long")
            return
        self.url = url

    def on_header(self, name: bytes, value: bytes) -> None:
        if self._oversized:
            return
        field_size = len(name) + len(value)
        if (
            field_size > MAX_HEADER_SIZE
            or self._header_bytes_total + field_size > MAX_TOTAL_HEADERS_SIZE
        ):
            self._reject_oversized(
                status.HTTP_431_REQUEST_HEADER_FIELDS_TOO_LARGE,
                b"Request Header Fields Too Large",
            )
            return
        self._header_bytes_total += field_size
        # Trailer fields of a chunked body arrive here after headers-complete.
        # They belong to the in-flight message, not the next pipelined
        # request's header block, and nothing downstream consumes them - drop
        # them once they have been counted against the size caps above, and
        # before the Content-Length capture below so a trailer named
        # `content-length` cannot poison the next request's early-413 guard.
        if self._headers_done:
            return
        name = name.lower()
        # Capture the two headers the dispatch path needs so headers-complete
        # reads a slot instead of rescanning the list. `Content-Length` is kept
        # raw and parsed lazily; `Expect: 100-continue` is a case-insensitive
        # token (RFC 9110 section 10.1.1). On a (malformed) duplicate
        # `Content-Length`, the first value wins for the early-413 size guard.
        if name == RAW_HEADER_CONTENT_LENGTH:
            if self._raw_content_length is None:
                self._raw_content_length = value
        elif name == b"expect" and value.strip().lower() == b"100-continue":
            self._has_expect_continue = True
        self.headers.append((name, value))

    def on_headers_complete(self) -> None:
        """Headers are fully parsed - build the Request and dispatch it now.

        The method, path, query and headers are all known at this point, so
        the request is constructed and enqueued before its body arrives. A
        `RequestBodySource` is attached; `on_body` feeds it incrementally and
        `on_message_complete` signals EOF. This is the headers-complete
        dispatch model: a handler that streams the body sees chunks as the
        socket delivers them rather than waiting for the whole body to buffer.
        """
        if self._oversized:
            return
        if self.transport is None or self.transport.is_closing():
            return

        # RFC 6455 native upgrade (Sec. 4.2). Gated on the parser's C-level
        # `should_upgrade()` flag, which is set only when httptools parsed a
        # `Connection: upgrade` + `Upgrade:` request - so an ordinary GET never
        # enters the Python-level header scan inside `_handle_websocket_upgrade`.
        # A valid upgrade with a matching websocket route is diverted there and
        # returns. Any other upgrade (h2c, an unknown protocol token, a non-GET
        # upgrade) is one this server does not speak: reject it with 400 and stop
        # here. The request must NOT be enqueued - httptools raises
        # `HttpParserUpgrade` at the body offset for any upgrade request, so
        # dispatching here would run the handler for a request the client is told
        # failed (the 400 is written from `data_received`'s no-websocket branch).
        if self.parser.should_upgrade():
            if not self._handle_websocket_upgrade():
                self._send_bad_request()
            return

        max_len = self.app.config.get("MAX_CONTENT_LENGTH")
        # Reject on the declared Content-Length before reading a single body
        # byte - cheapest possible 413 for an honest client that announces an
        # over-limit upload up front.
        if max_len is not None:
            declared = self._declared_content_length()
            if declared is not None and declared > max_len:
                self._reject_413()
                return

        # Clear an `Expect: 100-continue` client to send its body. The early
        # 413 above already rejected an over-limit declared Content-Length, so
        # we never invite a body we are about to refuse. RFC 9110 section
        # 10.1.1 forbids the interim to an HTTP/1.0 client, so it is gated on
        # the request being HTTP/1.1.
        # `transport` is already non-None here (guarded at method entry, and
        # this is a synchronous callback with no await in between), but the
        # explicit check keeps every transport.write site uniformly guarded.
        if self._wants_continue() and self.transport is not None:
            self.transport.write(b"HTTP/1.1 100 Continue\r\n\r\n")

        keep_alive = self.parser.should_keep_alive()
        # The slowloris guard stays armed: the body may still be arriving, and
        # a stalled body must still time out. It is stood down only at
        # message-complete. The idle keep-alive timer was already cancelled
        # when the first bytes arrived (`_arm_request_timer`).

        path, query_bytes = self._parse_request_target()
        query_string = query_bytes.decode("ascii")

        source = RequestBodySource(max_content_length=max_len)
        # Wire body backpressure to the transport's flow control: the source
        # pauses reading when its buffer hits the high-water mark and resumes
        # once a consumer drains it back down. Bounds the per-connection body
        # buffer regardless of how fast the kernel delivers bytes.
        source.set_flow_control(self._pause_reading, self._resume_reading)
        request = Request(
            method=self.parser.get_method().decode("ascii"),
            path=path,
            query_string=query_string,
            headers=self.headers,
            body=b"",
            transport=self.transport,
            body_source=source,
        )
        # The parser keeps advancing through pipelined bytes, so the URL /
        # header buffers must be cleared now - a follow-up request's on_url /
        # on_header would otherwise append into the same live lists. The
        # already-built Request holds its own copies.
        self._current_source = source
        self.url = b""
        self.headers = []
        self._header_bytes_total = 0
        self._headers_done = True
        self._raw_content_length = None
        self._has_expect_continue = False
        # Match ONCE here and thread the result into `handle_request`, exactly
        # as `_asgi_app` does, so the radix tree is not walked twice. `_dispatch`
        # also needs the match before the handler starts, to know whether this
        # route streams its body or wants it buffered.
        match = self.app.match(request.method, request.path)
        self._request_queue.append((request, source, keep_alive, match))
        # Start the per-connection server loop on the first queued request; it
        # runs until the queue drains, guaranteeing FIFO response ordering.
        if self._server_loop is None or self._server_loop.done():
            self._server_loop = self.loop.create_task(self._serve())
            HttpProtocol._active_tasks.add(self._server_loop)
            self._server_loop.add_done_callback(self._task_done)

    # ── WebSocket upgrade (RFC 6455 Sec. 4.2) ─────────────

    def _handle_websocket_upgrade(self) -> bool:
        """Attempt the RFC 6455 handshake; return True if the connection diverted.

        Called from `on_headers_complete` when the parser flagged an upgrade.
        Detects a valid upgrade request, matches a websocket route, runs the
        host/Origin checks, sends the 101, and diverts the connection into
        WebSocket mode. Returns True once the connection has been handled
        (diverted, or terminated with an HTTP error response); False means "not a
        WebSocket upgrade - fall through to the normal HTTP path".
        """
        # RFC 6455 Sec. 4.1 requires the handshake to be a GET. A non-GET upgrade
        # (e.g. an HTTP/2 prior-knowledge or h2c attempt) is not a WebSocket
        # handshake - fall through to the normal HTTP path, which rejects the
        # protocol switch with a 400 like any other non-WebSocket upgrade.
        if self.parser.get_method() != b"GET":
            return False
        # Detect the upgrade triplet (RFC 6455 Sec. 4.2.1). `connection` may be a
        # comma list ("keep-alive, Upgrade"); the others are single tokens. All
        # header names are already lowercased by `on_header`.
        has_upgrade_token = False
        upgrade_is_ws = False
        ws_key: bytes | None = None
        ws_version: bytes | None = None
        host = b""
        origin: bytes | None = None
        for name, value in self.headers:
            if name == _WS_HEADER_CONNECTION:
                # Case-insensitive, comma-list aware: any "upgrade" token counts.
                for token in value.split(b","):
                    if token.strip().lower() == _WS_HEADER_UPGRADE:
                        has_upgrade_token = True
                        break
            elif name == _WS_HEADER_UPGRADE:
                if value.strip().lower() == b"websocket":
                    upgrade_is_ws = True
            elif name == _WS_HEADER_KEY:
                if value:
                    ws_key = value
            elif name == _WS_HEADER_VERSION:
                ws_version = value.strip()
            elif name == _WS_HEADER_HOST and not host:
                host = value
            elif name == _WS_HEADER_ORIGIN and origin is None:
                origin = value

        # Not a WebSocket handshake at all - fall through to HTTP. Short-circuit
        # on the first missing element so an ordinary GET pays almost nothing.
        if not (has_upgrade_token and upgrade_is_ws):
            return False

        transport = self.transport
        if transport is None or transport.is_closing():
            return True

        # An upgrade to websocket with the wrong version: per RFC 6455 Sec. 4.2.2
        # respond 426 advertising the version we speak.
        if ws_version != _WS_SUPPORTED_VERSION:
            self._write_ws_http_error(
                status.HTTP_426_UPGRADE_REQUIRED,
                b"Upgrade Required",
                extra=b"Sec-WebSocket-Version: 13\r\n",
            )
            return True

        # A malformed upgrade missing the mandatory key (RFC 6455 Sec. 4.2.1) is
        # a bad request - there is no nonce to fingerprint into the 101.
        if ws_key is None:
            self._write_ws_http_error(status.HTTP_400_BAD_REQUEST, b"Bad Request")
            return True

        path, query_bytes = self._parse_request_target()

        # Host / Origin allow-lists first, before the route table is consulted.
        # An HTTP middleware's `process_request` never sees a handshake, so the
        # allow-lists are applied here. Matching first told an origin the app has
        # already decided not to trust whether a path exists - a 404 for an
        # unknown one, a 403 for a known one - and the ASGI path gated first, so
        # the two transports also disagreed about which refusal a request drew.
        # A rejection here precedes the 101 (the upgrade has not completed), so
        # it is an HTTP 403, not a close frame.
        host_str = _extract_host(host.decode("latin-1")) if host else ""
        origin_str = origin.decode("latin-1") if origin is not None else ""
        if _ws_handshake_rejection(self.app._middlewares, host_str, origin_str):
            self._write_ws_http_error(status.HTTP_403_FORBIDDEN, b"Forbidden")
            return True

        # Match BEFORE sending the 101 so we never switch protocols on a path
        # with no handler. No handler -> 404 (RFC-correct: the upgrade has not
        # completed, so the refusal is an ordinary HTTP response, not a close
        # frame).
        ws_match = self.app.match(ROUTE_METHOD_WEBSOCKET, path)
        if ws_match is None:
            self._write_ws_http_error(status.HTTP_404_NOT_FOUND, b"Not Found")
            return True

        # Send the 101 (RFC 6455 Sec. 4.2.2) synchronously to switch the byte
        # stream. The accept-key math is the single shared `compute_accept`.
        accept_key = compute_accept(ws_key.decode("latin-1"))
        transport.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept_key.encode("ascii") + b"\r\n\r\n"
        )

        # Build the raw-transport WebSocket. The scope mirrors the ASGI websocket
        # shape so the same path/query/client/cookies accessors work.
        headers_dict = {
            name.decode("latin-1"): value.decode("latin-1") for name, value in self.headers
        }
        scope = {
            "type": "websocket",
            "path": path,
            "query_string": query_bytes,
            "headers": self.headers,
            "client": transport.get_extra_info("peername"),
        }
        idle_timeout = self.app.config.get("WEBSOCKET_IDLE_TIMEOUT")
        ws = WebSocket.from_transport(
            transport,
            headers_dict,
            scope,
            path_params=ws_match.path_params,
            idle_timeout=idle_timeout,
        )
        # Wire write-side backpressure: the raw send path awaits this before each
        # frame, so a slow-reading client (which trips `pause_writing`) suspends
        # the producing handler instead of growing the transport write buffer
        # without bound. Mirrors the read-side `source.set_flow_control` wiring.
        ws.set_send_drain(self.drain)

        # Divert: all subsequent socket bytes go to the frame parser. Stand down
        # the HTTP timers and clear the consumed request buffers - no HTTP
        # Request is built or enqueued for this connection.
        self._websocket = ws
        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None
        self.url = b""
        self.headers = []
        self._header_bytes_total = 0
        self._raw_content_length = None
        self._has_expect_continue = False

        # Dispatch the handler through the shared core. Tracked in `_active_tasks`
        # with the generic done-callback (logs unhandled errors) and held in
        # `_ws_task` so connection_lost can cancel it if the client drops.
        self._ws_task = self.loop.create_task(self.app._run_websocket(ws, ws_match.route_info))
        HttpProtocol._active_tasks.add(self._ws_task)
        self._ws_task.add_done_callback(functools.partial(self._ws_task_done, ws))
        return True

    def _write_ws_http_error(self, status_code: int, reason: bytes, extra: bytes = b"") -> None:
        """Refuse a handshake with a plain HTTP response, then close.

        Used for the pre-101 refusal paths (wrong version, no route, host/Origin
        rejected, malformed). The upgrade has not completed, so the refusal is an
        ordinary HTTP/1.1 response (RFC 6455 Sec. 4.2.2 / Sec. 4.4), never a
        WebSocket close frame. `extra` carries any additional header lines (e.g.
        the 426's `Sec-WebSocket-Version`).
        """
        self._oversized = True
        self._emit_http_error(status_code, reason, extra=extra)

    def on_body(self, body: bytes) -> None:
        # Feed the in-flight request's body source. The source is the single
        # source of truth for the running byte total: `feed` tracks it and
        # flips an overflow latch past MAX_CONTENT_LENGTH, so the handler's
        # next read raises 413. We short-circuit the transport here too, so an
        # over-reading client is dropped promptly rather than allowed to keep
        # streaming megabytes we will only discard.
        source = self._current_source
        if source is None:
            return
        source.feed(body)
        if source.overflowed:
            self._reject_413()

    def on_message_complete(self) -> None:
        # The message (incl. any chunked trailers) is over: the next on_url /
        # on_header callbacks belong to a pipelined follow-up, so reopen the
        # header phase and zero the size budget that trailers counted against.
        self._headers_done = False
        self._header_bytes_total = 0
        # The body finished arriving in time - stand the slowloris guard down
        # and signal EOF to the in-flight source so a streaming consumer ends.
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None
        # The timer is now None; a pipelined follow-up's first bytes re-arm the
        # slowloris timer in data_received purely off `_request_timer is None`.
        if self._current_source is not None:
            self._current_source.feed_eof()
            self._current_source = None

    # ── asyncio.Protocol callbacks ────────────────────────

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # HTTP/WebSocket runs over a full-duplex transport; the Liskov-correct
        # signature widens to `BaseTransport`, so narrow back here. Check by
        # capability, not `isinstance(asyncio.Transport)`: uvloop's transport
        # implements the full-duplex interface but is NOT a subclass of
        # `asyncio.Transport`, so an isinstance check rejects the production
        # uvloop loop (every connection fails). A full-duplex transport has both
        # `write` (write side) and `pause_reading` (read side); a half-duplex
        # one lacks one.
        # Explicit raise (not `assert`) so `python -O` does not strip it.
        if not (
            callable(getattr(transport, "write", None))
            and callable(getattr(transport, "pause_reading", None))
        ):
            raise RuntimeError(
                f"expected a full-duplex transport (write + pause_reading), "
                f"got {type(transport).__name__}"
            )
        # Narrow the local now so the 503-reject write/close below and the
        # assignment see a `Transport`; the capability check above already
        # proved the full-duplex interface is present.
        transport = cast("asyncio.Transport", transport)
        self.transport = transport

        # Per-process connection cap (DDoS guard). Admit-or-reject decision
        # is taken under the lock so a burst of parallel connection_made
        # calls cannot all observe `count == cap - 1` and over-admit.
        cap = self.app.config.get("MAX_CONCURRENT_CONNECTIONS", DEFAULT_MAX_CONCURRENT_CONNECTIONS)
        with HttpProtocol._connections_lock:
            if HttpProtocol._active_connections >= cap:
                admitted = False
            else:
                HttpProtocol._active_connections += 1
                admitted = True
                self._counted = True
        if not admitted:
            self._emit_http_error(status.HTTP_503_SERVICE_UNAVAILABLE, b"Service Unavailable")
            return

        # Arm write-side flow control: asyncio fires `pause_writing` once the
        # buffer exceeds the high mark and `resume_writing` when it drains below
        # the low mark, which the streaming path awaits via `drain()`. Without a
        # set high mark the proactor/selector defaults can be large, so set it
        # explicitly. Wrapped because some transports do not implement it.
        high = self.app.config.get("WRITE_BUFFER_HIGH_WATER", WRITE_BUFFER_HIGH_WATER)
        with contextlib.suppress(NotImplementedError, AttributeError):
            transport.set_write_buffer_limits(high=high)

        # Enable OS-level TCP keepalive so the kernel detects a peer that died
        # without closing (no FIN, no further bytes) - the application idle
        # timer never sees such a connection. Skipped when disabled in config or
        # when no settable socket is reachable: a transport may expose no
        # `get_extra_info` (some test/uvloop stand-ins) or return None for the
        # socket (a TLS transport). The socket is duck-typed on `setsockopt`
        # rather than `isinstance(socket.socket)` so a uvloop socket wrapper is
        # accepted too. Platform differences in the tuning options are handled
        # inside the helper.
        if self.app.config.get("TCP_KEEPALIVE", True):
            get_extra_info = getattr(transport, "get_extra_info", None)
            sock = get_extra_info("socket") if callable(get_extra_info) else None
            if callable(getattr(sock, "setsockopt", None)):
                _enable_tcp_keepalive(
                    cast("_socket.socket", sock),
                    self.app.config.get("TCP_KEEPALIVE_IDLE"),
                    self.app.config.get("TCP_KEEPALIVE_INTERVAL"),
                    self.app.config.get("TCP_KEEPALIVE_COUNT"),
                )

        # Track this connection so graceful shutdown can flip its drain flag.
        # If shutdown already began before this connection was admitted, start
        # it draining immediately so it serves at most its first request.
        HttpProtocol._live_connections.add(self)
        if _SHUTTING_DOWN:
            self._draining = True

        self._start_keep_alive_timer()

    def connection_lost(self, exc: Exception | None) -> None:
        if self._counted:
            with HttpProtocol._connections_lock:
                HttpProtocol._active_connections -= 1
            self._counted = False
        HttpProtocol._live_connections.discard(self)
        # WebSocket mode: the client dropped. Mark the connection closed so a
        # handler blocked in `receive` unwinds on its next read, and cancel the
        # dispatch task; `_run_websocket`'s finally still runs its teardowns.
        if self._websocket is not None:
            self._websocket._closed = True
            if self._ws_task is not None and not self._ws_task.done():
                self._ws_task.cancel()
            self._websocket = None
            self._ws_task = None
            self.transport = None
            return
        # Stop the server loop from dispatching further queued requests and
        # drop any not-yet-served pipelined work; the client is gone.
        self._closing = True
        # Release a stream parked in `drain()`; with the transport gone the
        # streaming loop's next write fails fast instead of hanging forever.
        self._can_write.set()
        self._request_queue.clear()
        # Unblock any consumer awaiting more body on the in-flight source -
        # the client is gone, so its body will never complete. The server
        # loop is cancelled below; signalling EOF here makes a streaming read
        # end cleanly even on the cancellation race.
        if self._current_source is not None:
            self._current_source.feed_eof()
            self._current_source = None
        if self._server_loop is not None and not self._server_loop.done():
            self._server_loop.cancel()
        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None
        self.transport = None

    # ── graceful shutdown ─────────────────────────────────

    def begin_drain(self) -> None:
        """Mark this connection for graceful quiescing.

        The in-flight request (if any) runs to completion; the `_serve` loop
        then closes the transport at the request boundary rather than serving
        further pipelined/queued requests. An idle keep-alive connection with
        no in-flight request and nothing queued is closed at once so it does
        not linger waiting out its idle timer during shutdown.
        """
        self._draining = True
        idle = (self._server_loop is None or self._server_loop.done()) and not self._request_queue
        if idle and self.transport is not None and not self.transport.is_closing():
            self._closing = True
            if self._keep_alive_handle is not None:
                self._keep_alive_handle.cancel()
                self._keep_alive_handle = None
            self.transport.close()

    @classmethod
    def start_graceful_drain(cls) -> None:
        """Phase one of graceful shutdown: quiesce every live connection.

        Sets the process latch so connections admitted during the shutdown
        window start draining immediately, then flips the drain flag on every
        currently-live connection. Each connection self-quiesces at its own
        request boundary - no eager per-connection close racing a mid-write
        transport. A snapshot of the live set is taken first because
        `begin_drain` may close (and thus deregister) a connection, mutating
        the set mid-iteration.
        """
        global _SHUTTING_DOWN
        _SHUTTING_DOWN = True
        for conn in list(cls._live_connections):
            conn.begin_drain()

    @classmethod
    def reset_graceful_drain(cls) -> None:
        """Clear the shutdown latch. For test suites driving shutdown repeatedly."""
        global _SHUTTING_DOWN
        _SHUTTING_DOWN = False

    def _arm_request_timer(self) -> None:
        """Start the slowloris read budget when a request's bytes begin
        arriving. The connection is no longer idle, so the keep-alive
        timer is stood down in favour of the (shorter) request timer."""
        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        timeout = self.app.config.get("REQUEST_TIMEOUT", self.REQUEST_TIMEOUT)
        self._request_timer = self.loop.call_later(timeout, self._request_timeout)

    def _pause_reading(self) -> None:
        """Stop pulling bytes off the socket - the body buffer is full.

        Invoked by the in-flight `RequestBodySource` when its buffer reaches
        the high-water mark. A guard keeps it safe after teardown: a closing or
        absent transport simply ignores the request.
        """
        transport = self.transport
        if transport is not None and not transport.is_closing():
            transport.pause_reading()

    def _resume_reading(self) -> None:
        """Resume pulling bytes once the consumer drains below the low mark."""
        transport = self.transport
        if transport is not None and not transport.is_closing():
            transport.resume_reading()

    # ── write-side flow control ───────────────────────────

    def pause_writing(self) -> None:
        """asyncio callback: the transport write buffer crossed the high mark.

        Clearing the gate makes the next `drain()` block, throttling a
        producer (a streaming/SSE response or the native WebSocket send path)
        that is outrunning a slow client. Idempotent - asyncio guarantees
        paired pause/resume, but tolerating a repeat avoids the
        crash-on-double-pause assert aiohttp carries.
        """
        self._can_write.clear()

    def resume_writing(self) -> None:
        """asyncio callback: the write buffer drained below the low mark."""
        self._can_write.set()

    async def drain(self) -> None:
        """Block while the transport write buffer is over the high mark.

        Returns immediately on the common path (gate set). The streaming and
        SSE response paths, and the native WebSocket send path, await this
        after writing each chunk/frame so a fast producer cannot grow the
        event loop's write buffer without bound. A closing/absent transport
        returns at once so a torn-down connection does not park the producer.
        """
        if self._can_write.is_set():
            return
        transport = self.transport
        if transport is None or transport.is_closing():
            return
        await self._can_write.wait()

    def _emit_http_error(
        self, status_code: int, reason: bytes, body: bytes = b"", extra: bytes = b""
    ) -> None:
        """Write a minimal `Connection: close` HTTP/1.1 error response, then close.

        The single framing path for every pre-dispatch refusal (oversized
        request line/headers, 413, 408, 400, the 503 admission reject, and the
        pre-101 WebSocket handshake refusals). `Content-Length` is derived from
        `body`; `extra` carries any additional header lines (e.g. the 426's
        `Sec-WebSocket-Version`). A guard short-circuits when the transport is
        already gone so a torn-down connection is never written to. Caller-
        specific side effects (the `_oversized` latch, source EOF, timer resets)
        stay in the callers - this helper owns only the wire framing.
        """
        transport = self.transport
        if transport is None or transport.is_closing():
            return
        phrase = reason.decode("ascii")
        head = (
            f"HTTP/1.1 {status_code} {phrase}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n"
        ).encode("ascii")
        transport.write(head + extra + b"\r\n" + body)
        transport.close()

    def _reject_oversized(self, status_code: int, reason: bytes) -> None:
        """Emit a minimal HTTP/1.1 error response and close the connection.

        Used when the request line or headers exceed configured caps; we
        can't trust the parser to recover, so the connection is terminated.
        """
        self._oversized = True
        self._emit_http_error(status_code, reason)

    def _parse_request_target(self) -> tuple[str, bytes]:
        """Split the parsed request line into `(path, raw_query_bytes)`.

        `path` is percent-decoded, as the ASGI scope's `path` is, so
        `/items/a%20b` binds `"a b"` on this transport too - it bound the raw
        `"a%20b"` before, and the same app answered differently depending on
        how it was served. The decode is skipped when the target carries no
        `%`, which is almost every request, so the common path pays one
        C-level scan of the raw bytes and no allocation.

        The query is returned raw so the WebSocket scope can carry the bytes
        verbatim while the HTTP path decodes them. Shared by the HTTP dispatch
        and the WebSocket upgrade so the request-target split lives in one
        place.
        """
        parsed = httptools.parse_url(self.url)
        raw_path = parsed.path
        if not raw_path:
            return "/", parsed.query or b""
        if _PERCENT_BYTE in raw_path:
            # `unquote` on the decoded text, matching what an ASGI server puts
            # in `scope["path"]`; a malformed escape is left as written rather
            # than raising, which is also what that path does.
            return unquote(raw_path.decode("ascii")), parsed.query or b""
        return raw_path.decode("ascii"), parsed.query or b""

    def _declared_content_length(self) -> int | None:
        """Parse the just-parsed request's `Content-Length` header, or None.

        Read at headers-complete off the value captured in `on_header`, so an
        over-limit upload can be refused before its body is read. Returns None
        when absent or malformed (the streamed running-total cap is the backstop
        in that case).
        """
        raw = self._raw_content_length
        if raw is None:
            return None
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

    def _wants_continue(self) -> bool:
        """Return whether the just-parsed request asks for a 100 Continue.

        True only for an HTTP/1.1 request carrying `Expect: 100-continue`. RFC
        9110 section 10.1.1 forbids sending the interim response to an HTTP/1.0
        client, so the version is checked first. The `Expect` value is a
        case-insensitive token; headers were already lowercased in `on_header`.
        Older httptools builds may not expose `get_http_version`; treat its
        absence as "do not send".
        """
        try:
            if self.parser.get_http_version() != "1.1":
                return False
        except (AttributeError, RuntimeError):
            return False
        return self._has_expect_continue

    def _reject_413(self) -> None:
        """Emit a 413 and close - the body exceeds MAX_CONTENT_LENGTH.

        Marks `_oversized` so any buffered follow-up parser callbacks
        short-circuit; the connection is terminated rather than trusted to
        resynchronise after a partially-read over-limit body.
        """
        self._oversized = True
        if self._current_source is not None:
            self._current_source.feed_eof()
            self._current_source = None
        # The same body the ASGI path answers with, so one app does not describe
        # the same refusal two ways depending on how it is served.
        body = orjson.dumps(too_large_payload(self.app.config.get("MAX_CONTENT_LENGTH")))
        self._emit_http_error(
            status.HTTP_413_CONTENT_TOO_LARGE,
            b"Content Too Large",
            body,
            extra=b"Content-Type: application/json\r\n",
        )

    def _request_timeout(self) -> None:
        """A client took too long to send a complete request - drop it."""
        self._request_timer = None
        self._emit_http_error(
            status.HTTP_408_REQUEST_TIMEOUT, b"Request Timeout", b"Request Timeout"
        )

    def _start_keep_alive_timer(self) -> None:
        """Start idle timeout - close connection if no request arrives."""
        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
        timeout = self.app.config.get("KEEP_ALIVE_TIMEOUT", self.KEEP_ALIVE_TIMEOUT)
        self._keep_alive_handle = self.loop.call_later(timeout, self._keep_alive_timeout)

    def _keep_alive_timeout(self) -> None:
        """Close idle connection after timeout."""
        if self.transport and not self.transport.is_closing():
            self.transport.close()

    @staticmethod
    def _task_done(task: asyncio.Task) -> None:
        """Callback for completed dispatch tasks - log errors, remove reference."""
        HttpProtocol._active_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _logger.error("Unhandled error in request dispatch: %s", exc, exc_info=exc)

    @staticmethod
    def _ws_task_done(ws: WebSocket, task: asyncio.Task) -> None:
        """Callback for a completed WebSocket dispatch task.

        A handler that raises closes with 1011 first, and that close awaits. A
        peer that has already gone brings `connection_lost` in to cancel this
        task mid-handshake, so the task ends *cancelled* and its exception is
        gone - the failure would be reported nowhere. `_run_websocket` records
        it on the socket before the close for exactly this case, so read it back
        rather than treating a cancellation as nothing to report.
        """
        HttpProtocol._active_tasks.discard(task)
        exc = ws._handler_exc if task.cancelled() else task.exception()
        if exc is not None:
            _logger.error("Unhandled error in websocket handler: %s", exc, exc_info=exc)

    def data_received(self, data: bytes) -> None:
        # Once the connection has diverted to WebSocket mode, every byte is a
        # frame (or part of one) - feed the frame parser, never the HTTP parser.
        # A close frame inside the buffer sets `_closed`, wakes any parked
        # receiver (so the handler unwinds via `WebSocketDisconnect` and the
        # close handshake completes), and raises `WebSocketDisconnect` out of
        # `feed_data`, which is suppressed here.
        if self._websocket is not None:
            with contextlib.suppress(WebSocketDisconnect):
                self._websocket.feed_data(data)
            return
        if self._oversized:
            return
        # First bytes of a fresh request - arm the slowloris read budget. The
        # timer is None between requests (cancelled at on_message_complete), so
        # a pipelined follow-up's first bytes re-arm it here.
        if self._request_timer is None:
            self._arm_request_timer()
        try:
            self.parser.feed_data(data)
        except httptools.HttpParserUpgrade as upgrade:
            # A successful WebSocket upgrade: `on_headers_complete` already
            # diverted the connection (`_handle_websocket_upgrade` set
            # `self._websocket`) and httptools then raises `HttpParserUpgrade`
            # at the body offset to signal the protocol switch. This is NOT an
            # error - suppress it so it does not surface through the event
            # loop's exception handler as a spurious traceback on every connect.
            # If the divert did not take (no route, refused handshake, or an
            # upgrade we do not speak), `on_headers_complete` already wrote the
            # refusal and closed; nothing more to do here.
            if self._websocket is not None:
                # The exception's argument is the offset into `data` at which
                # the post-handshake body begins. When the client pipelines its
                # first WebSocket frame into the same TCP segment as the
                # handshake, those bytes sit after the offset and the HTTP parser
                # never delivers them - feed them to the frame parser so the
                # first message is not dropped.
                offset = upgrade.args[0]
                if offset < len(data):
                    with contextlib.suppress(WebSocketDisconnect):
                        self._websocket.feed_data(data[offset:])
        except httptools.HttpParserError:
            self._send_bad_request()

    def _send_bad_request(self) -> None:
        """Write a minimal `400 Bad Request` and close the connection."""
        self._emit_http_error(status.HTTP_400_BAD_REQUEST, b"Bad Request", b"Bad Request")

    # ── request dispatch ──────────────────────────────────

    async def _serve(self) -> None:
        """Per-connection server loop: dispatch queued requests one at a time.

        Awaiting each `_dispatch` to completion before pulling the next request
        enforces HTTP/1.1 FIFO response ordering and bounds in-flight work to a
        single request per connection. The loop exits when the queue drains or
        the connection is being torn down.
        """
        while self._request_queue and not self._closing:
            request, source, keep_alive, match = self._request_queue.popleft()
            should_continue = await self._dispatch(request, source, keep_alive, match)
            # Notify the optional per-request hook (gunicorn max_requests
            # recycling) once the request has been fully dispatched, regardless
            # of whether the connection is kept alive or closed. Cheap None
            # check on the common (hook-unset) path; failures in the hook must
            # never break serving.
            hook = HttpProtocol.on_request_complete
            if hook is not None:
                try:
                    hook()
                except Exception:
                    _logger.exception("on_request_complete hook raised")
            if not should_continue:
                # Connection: close (or a failed write) - stop serving.
                return
            # Graceful-shutdown quiescing: the in-flight request just finished,
            # so close at this boundary instead of serving the next pipelined
            # request. The request that completed was honoured in full; queued
            # follow-ups are declined and the client retries on a fresh
            # connection against a still-serving worker.
            if self._draining:
                if self.transport is not None and not self.transport.is_closing():
                    self.transport.close()
                return
            # Honour worker recycling at the request boundary. When the gunicorn
            # worker has tripped max_requests (clearing its `alive` flag) the
            # predicate returns False, so the connection stops here rather than
            # draining further queued/pipelined requests past the limit. The
            # transport is closed so the client opens a fresh connection against
            # the replacement worker; failures in the predicate keep serving.
            keep = HttpProtocol.should_keep_serving
            if keep is not None:
                try:
                    serve_next = keep()
                except Exception:
                    serve_next = True
                    _logger.exception("should_keep_serving hook raised")
                if not serve_next:
                    if self.transport is not None and not self.transport.is_closing():
                        self.transport.close()
                    return
        # Queue drained on a keep-alive connection: rearm the idle timer so the
        # connection is reaped if no further request arrives. Skip the rearm if a
        # follow-up is mid-receive (_request_timer live) or already queued - the
        # slowloris timer governs that request, and arming the idle timer too
        # would break the keep-alive-XOR-request-timer invariant and could close
        # the transport mid-request.
        if (
            not self._closing
            and self.transport is not None
            and not self.transport.is_closing()
            and self._request_timer is None
            and not self._request_queue
        ):
            self._start_keep_alive_timer()

    async def _dispatch(
        self,
        request: Request,
        source: RequestBodySource,
        keep_alive: bool,
        match: RouteMatch | None = None,
    ) -> bool:
        """Dispatch one request and write its response.

        The Request was built at headers-complete; its body streams into
        `source` as the parser delivers it. After the handler returns (and its
        response is written) the source is drained to EOF - a handler that
        ignored the body must not strand unparsed bytes that would corrupt the
        next pipelined request or wedge the connection.

        Returns whether the connection should keep serving subsequent requests
        (True for keep-alive, False when the connection was or must be closed).
        """
        timeout = self.app.config.get("REQUEST_HANDLER_TIMEOUT", 30)
        # Hold the handler as an explicit task so that, on a timeout/error,
        # `asyncio.shield` can keep it RUNNING (so yield-dep cleanup and
        # teardown_request hooks finish) while we still hold a handle to know
        # when it eventually completes. A handler parked in
        # `async for chunk in request.stream()` is awaiting `source`; draining
        # that same source inline would create a SECOND waiter racing the live
        # handler on the source's single-waiter event - truncating its read and
        # thrashing the buffer. So we only ever drain when no consumer is alive.
        # A non-streaming route wants its body already buffered, so the sync
        # `.data` / `.get_json()` / `.form` accessors find it - the same
        # guarantee `_asgi_app` gives by draining inline before dispatch. Only a
        # `stream=True` route keeps the lazy source it consumes itself.
        #
        # The whole request usually arrives in one segment, so the parser has
        # already fed EOF by the time this runs: `at_eof` settles it with an
        # attribute read and no coroutine, leaving the bodyless-GET path exactly
        # as cheap as before. Only a body still in flight costs an await.
        if match is None or not match.route_info.stream:
            # `at_eof` plus a zero running total means the parser signalled EOF
            # without ever feeding a byte, so the body is empty and already
            # correct on the Request. Testing the total as well keeps this true
            # even if something upstream ever consumes from the source before
            # dispatch: it would fall to the await, which is merely slower.
            if source.at_eof and not source.total_bytes:
                request._mark_body_buffered()
            else:
                # Buffering is best-effort: an over-limit body latches
                # `overflowed` on the source, and re-raising here would render
                # the refusal as a bare 500, because the app's exception
                # handlers (which turn this into 413) live inside
                # `handle_request`. Swallow it and dispatch anyway - the
                # handler's own body access raises the same error where those
                # handlers can see it, and a handler that never reads the body
                # is refused by the same check it always was.
                with contextlib.suppress(RequestEntityTooLarge):
                    await request._drain_body()

        inner = self.loop.create_task(self.app.handle_request(request, match=match))
        detached = False
        try:
            # `asyncio.shield` lets the handler's finally-block teardowns run to
            # completion even when wait_for cancels on timeout. Without it,
            # CancelledError propagates into async teardowns and can interrupt
            # resource cleanup (DB connections, file handles).
            response = await asyncio.wait_for(asyncio.shield(inner), timeout=timeout)
        except asyncio.CancelledError:
            # The server loop itself was cancelled - the connection is being torn
            # down (client gone). The shield kept `inner` alive; there is no one
            # left to serve, so cancel it now rather than leaking a detached task.
            if not inner.done():
                inner.cancel()
            raise
        except asyncio.TimeoutError:
            response = Response(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                body=b"Gateway Timeout",
                content_type=MIME_TEXT_PLAIN,
            )
            detached = not inner.done()
        except Exception:
            _logger.exception("Unhandled exception in request dispatch")
            response = Response(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                body=MSG_INTERNAL_SERVER_ERROR.encode(),
                content_type=MIME_TEXT_PLAIN,
            )
            detached = not inner.done()

        if detached:
            # The handler is still alive after the shield (typically parked in
            # request.stream()). Draining here would race it on the source, so
            # defer cleanup: when the handler finally finishes, drain-and-discard
            # the body and close the source. The connection is NOT reused - the
            # parser/body state is mid-flight and the handler may still emit
            # reads - so we write the 504/500 and return False to close.
            HttpProtocol._active_tasks.add(inner)
            inner.add_done_callback(HttpProtocol._active_tasks.discard)
            inner.add_done_callback(self._on_detached_handler_done(source))
            return self._emit_and_close(response)

        # The handler completed (normally, or raised synchronously). No consumer
        # is awaiting the source, so it is safe to drain-and-discard any body it
        # left unread, keeping the parser's byte accounting correct for the next
        # pipelined request. EOF arrives via on_message_complete; on teardown the
        # source was already fed EOF in connection_lost / 413. A source already
        # at EOF (a bodiless GET, the common case) has nothing to discard, so skip
        # the no-op drain coroutine entirely.
        if not self._closing and not source.at_eof:
            await source.drain()

        if self.transport is None or self.transport.is_closing():
            return False

        # RFC 9110 Sec. 9.3.2: a HEAD response carries the same header section a
        # GET would (including the would-be Content-Length) but MUST NOT include
        # a message body. `Response.encode()` cannot see the request method, so
        # the body strip lives here, mirroring the ASGI emit path. Sending the
        # full body corrupts keep-alive framing - the client parses the body
        # bytes as the start of the next response.
        is_head = request.method == HTTP_METHOD_HEAD
        try:
            if getattr(response, "is_event_source", False):
                # The stream owns the connection and this path closes it when
                # the generator ends, so the head must say so rather than
                # advertising a socket the client may reuse.
                if is_head:
                    self.transport.write(response.encode(keep_alive=False))
                else:
                    await response.stream_to(self.transport, drain=self.drain, keep_alive=False)
                self.transport.close()
                return False
            # `is_streamed`, not a class test: a response that produces
            # chunks must be emitted as one whatever type it is, and testing
            # for `StreamingResponse` is what let `EventSourceResponse` and
            # then a streamed `FileResponse` fall to the buffered encoder.
            if response.is_streamed:
                if is_head:
                    # The encoded head advertises the (would-be) representation;
                    # a HEAD response stops there - no chunks, no chunked
                    # terminator (HEAD bodies are forbidden regardless of
                    # Transfer-Encoding).
                    self.transport.write(response.encode(keep_alive=keep_alive))
                else:
                    await response.stream_to(
                        self.transport, drain=self.drain, keep_alive=keep_alive
                    )
            elif is_head:
                self.transport.write(_strip_response_body(response.encode(keep_alive=keep_alive)))
            else:
                self.transport.write(response.encode(keep_alive=keep_alive))
        except Exception:
            _logger.exception(MSG_ERROR_RESPONSE_EMISSION)
            self.transport.close()
            return False

        if not keep_alive:
            self.transport.close()
            return False
        # Keep-alive: keep serving. The parser is intentionally NOT recreated -
        # it still holds buffered bytes for any pipelined follow-up request.
        self._reset()
        return True

    def _emit_and_close(self, response: Response) -> bool:
        """Write a plain error response and close the connection.

        Used on the timeout/error paths where the handler is still detached and
        alive: the connection cannot be safely reused, so the 504/500 is written
        and the transport closed. Returns False so the server loop stops serving.
        """
        transport = self.transport
        if transport is not None and not transport.is_closing():
            try:
                transport.write(response.encode(keep_alive=False))
            except Exception:
                _logger.exception(MSG_ERROR_RESPONSE_EMISSION)
            transport.close()
        return False

    def _on_detached_handler_done(
        self, source: RequestBodySource
    ) -> Callable[[asyncio.Task], None]:
        """Build a done-callback that drains the body once a detached handler ends.

        On a timeout/error the shielded handler keeps running so its teardowns
        finish. While it is alive it may still be the source's sole consumer, so
        the body must not be drained inline. This callback fires when the handler
        finally completes - at which point no consumer is awaiting the source -
        and drains-and-discards any unread body, then closes the source. The
        connection is already closing (the 504/500 path returned False), so this
        is purely buffer cleanup; failures are swallowed.
        """

        def _callback(task: asyncio.Task) -> None:
            with contextlib.suppress(Exception):
                task.exception()
            # Feed EOF so a drain that would otherwise wait for on_message_complete
            # (which may never fire on a closing connection) completes promptly,
            # then schedule the async drain on the loop.
            source.feed_eof()
            drain_task = self.loop.create_task(source.drain())
            HttpProtocol._active_tasks.add(drain_task)
            drain_task.add_done_callback(HttpProtocol._active_tasks.discard)

        return _callback

    def _reset(self) -> None:
        """Reset per-request scratch state for keep-alive connection reuse.

        Only state that cannot already belong to a started pipelined follow-up
        is cleared here. The URL / header buffers and their size counters are
        reset by on_headers_complete the moment a request is built - and a
        reused httptools parser may have already written a follow-up's on_url /
        on_header into those same live buffers by the time this runs (after the
        prior request's dispatch awaits). Clearing them here would destroy that
        follow-up's parse state and dispatch it with an empty URL, so they are
        left alone. Only _oversized (a per-connection reject latch that no
        normal follow-up sets) is cleared. The idle keep-alive timer is rearmed
        by the server loop once the request queue drains, not per request.
        """
        self._oversized = False
