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
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast
from urllib.parse import unquote

import httptools

import veloce.status as status
from veloce._constants import (
    MIME_JSON,
    MIME_TEXT_PLAIN,
    MSG_ERROR_RESPONSE_EMISSION,
    MSG_INTERNAL_SERVER_ERROR,
)
from veloce._internal import _extract_host, _ws_handshake_rejection, dumps_for
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
from veloce.http._body import RequestBodySource, body_too_large, too_large_payload
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.websocket import WebSocket, compute_accept

if TYPE_CHECKING:  # pragma: no cover
    from veloce._pipeline import CompiledPipeline
    from veloce.app import Veloce
    from veloce.routing.router import RouteMatch

_logger = logging.getLogger(__name__)


# Caps the request line and headers, so neither can pin RAM before MAX_CONTENT_LENGTH engages.
MAX_URL_SIZE = 8192
MAX_HEADER_SIZE = 8192
MAX_TOTAL_HEADERS_SIZE = 65536

WRITE_BUFFER_HIGH_WATER = DEFAULT_WRITE_BUFFER_HIGH_WATER

# Graceful-shutdown latch: a connection admitted after it is set drains at once.
_SHUTTING_DOWN = False

# A bitmask, so a resume for one reason cannot cancel the other's pause.
_PAUSE_BODY = 1
_PAUSE_DEPTH = 2


def _enable_tcp_keepalive(
    sock: _socket.socket,
    idle: int | None,
    interval: int | None,
    count: int | None,
) -> None:
    """Turn on OS-level TCP keepalive for an accepted connection socket.

    SO_KEEPALIVE lets the kernel reap a half-open connection that vanished
    without a FIN, which the application-level idle timer never observes.
    The tuning options are platform-specific - Windows exposes none of
    TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT - so each is probed with
    `getattr` and skipped when absent, and a `None` value leaves the OS default
    in place. Failures are swallowed: keepalive is best-effort hardening, never
    a reason to drop a serviceable connection.
    """
    with contextlib.suppress(OSError):
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
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

    A HEAD response keeps the header section (with the would-be Content-Length)
    but sends no body (RFC 9110 Sec. 9.3.2). The separator is always present.
    """
    return encoded[: encoded.index(b"\r\n\r\n") + 4]


# RFC 6455 Sec. 4.2.1: the upgrade triplet, lowercased as `on_header` delivers it.
_WS_HEADER_CONNECTION = b"connection"
_WS_HEADER_UPGRADE = b"upgrade"
_WS_HEADER_KEY = b"sec-websocket-key"
_WS_HEADER_VERSION = b"sec-websocket-version"
_WS_HEADER_HOST = b"host"
_WS_HEADER_ORIGIN = b"origin"

CONTENT_TYPE_JSON_LINE = b"Content-Type: application/json\r\n"


# RFC 6455 Sec. 4.2.2: the only WebSocket protocol version Veloce speaks.
_WS_SUPPORTED_VERSION = b"13"


#: `%` as an int: an int in `bytes` is a memchr, a one-byte `bytes` a substring search
#: an order of magnitude dearer on a per-request path.
_PERCENT_BYTE = 0x25


class _RequestCompleteHook(Protocol):
    def __call__(self) -> None: ...


class _KeepServingPredicate(Protocol):
    def __call__(self) -> bool: ...


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
        "_header_phase_bytes",
        "_headers_done",
        "_oversized",
        "_counted",
        "_request_queue",
        "_pause_reasons",
        "_max_pipelined",
        "_server_loop",
        "_closing",
        "_draining",
        "_current_source",
        "_serving_source",
        "_raw_content_length",
        "_has_expect_continue",
        "_can_write",
        "_websocket",
        "_ws_task",
        "__weakref__",
    )

    # Prevents GC of in-flight tasks across all connections.
    _active_tasks: set[asyncio.Task[Any]] = set()

    # Walked by graceful shutdown to flip every `_draining` flag; weak, so a torn-down
    # connection drops out by itself.
    _live_connections: weakref.WeakSet[HttpProtocol] = weakref.WeakSet()

    # Installed by the gunicorn worker to drive `max_requests` recycling.
    on_request_complete: ClassVar[_RequestCompleteHook | None] = None

    # Lets `max_requests` recycling stop at a request boundary rather than drain the queue.
    should_keep_serving: ClassVar[_KeepServingPredicate | None] = None

    # `+=` on a class-level int is read-modify-write, not atomic under the GIL.
    _active_connections: int = 0
    _connections_lock: threading.Lock = threading.Lock()

    # Fallbacks for a protocol built without an app config.
    KEEP_ALIVE_TIMEOUT = _Config.default_config()["KEEP_ALIVE_TIMEOUT"]
    # Slowloris guard: request line + headers + body must finish in this budget.
    REQUEST_TIMEOUT = _Config.default_config()["REQUEST_TIMEOUT"]

    # A bodiless GET fills no body buffer: unbounded, a peer queues one per 27 bytes.
    MAX_PIPELINED_REQUESTS = 64

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
        # Spent against bytes read: `on_header` fires only once a line ends.
        self._header_phase_bytes: int = 0
        # So chunked trailers (RFC 9112 Sec. 7.1.2) are not read as the next request's.
        self._headers_done: bool = False
        # Latched once a cap trips, so buffered callbacks cannot double-emit.
        self._oversized: bool = False
        # So a connection refused at the cap is not decremented on teardown.
        self._counted: bool = False
        self._pause_reasons = 0
        self._max_pipelined: int = app.config.get(
            "MAX_PIPELINED_REQUESTS", HttpProtocol.MAX_PIPELINED_REQUESTS
        )
        # HTTP/1.1 mandates FIFO ordering, so one loop drains this serially.
        self._request_queue: deque[tuple[Request, RequestBodySource, bool, RouteMatch | None]] = (
            deque()
        )
        self._server_loop: asyncio.Task[Any] | None = None
        # Set on teardown, so a client closing mid-pipeline cannot wedge the loop.
        self._closing: bool = False
        # A draining connection finishes its request; `_closing` stops at once.
        self._draining: bool = False
        # Not the source being dispatched: an earlier request may still be in flight.
        self._current_source: RequestBodySource | None = None
        # Separate: `_current_source` goes at message-complete, a stream outlives it.
        self._serving_source: RequestBodySource | None = None
        # Captured so headers-complete reads them without rescanning the list.
        self._raw_content_length: bytes | None = None
        self._has_expect_continue: bool = False
        # `drain()` blocks only while cleared, so keep-alive pays one check.
        self._can_write: asyncio.Event = asyncio.Event()
        self._can_write.set()
        # Once set, socket bytes go to the frame parser, not the HTTP parser.
        self._websocket: WebSocket | None = None
        self._ws_task: asyncio.Task[Any] | None = None

    # ── httptools callbacks ───────────────────────────────

    def on_url(self, url: bytes) -> None:
        """Accumulate the request target, which httptools may deliver in pieces."""
        if self._oversized:
            return
        # Appended and capped on the whole target: `on_url` is incremental.
        self.url += url
        if len(self.url) > MAX_URL_SIZE:
            self._reject_oversized(status.HTTP_414_REQUEST_URI_TOO_LONG, b"URI Too Long")
            return

    def on_header(self, name: bytes, value: bytes) -> None:
        """Record one header, refusing the request once the size budget is spent."""
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
        # Chunked trailers arrive here and nothing consumes them. Dropped before the
        # capture, so one named `content-length` cannot poison the early-413 guard.
        if self._headers_done:
            return
        name = name.lower()
        # `Expect: 100-continue` is a case-insensitive token (RFC 9110
        # Sec. 10.1.1); on a duplicate `Content-Length` the first value wins.
        if name == RAW_HEADER_CONTENT_LENGTH:
            if self._raw_content_length is None:
                self._raw_content_length = value
        elif name == b"expect" and value.strip().lower() == b"100-continue":
            self._has_expect_continue = True
        self.headers.append((name, value))

    def _reset_header_state(self) -> None:
        """Clear the per-message header-parse buffers and their derived flags.

        Called as soon as a request is built, because the parser keeps advancing
        through pipelined bytes and a follow-up's `on_url` / `on_header` would
        otherwise append into the live lists. `_headers_done` is deliberately
        not cleared here, and `on_message_complete` re-zeroes
        `_header_bytes_total` itself because chunked trailers keep charging that
        budget after this has run.
        """
        self.url = b""
        self.headers = []
        self._header_bytes_total = 0
        self._header_phase_bytes = 0
        self._raw_content_length = None
        self._has_expect_continue = False

    def on_headers_complete(self) -> None:
        """Headers are fully parsed - build the Request and dispatch it now.

        The request is constructed and enqueued before its body arrives, so a
        handler that streams the body sees chunks as the socket delivers them
        rather than waiting for the whole body to buffer.
        """
        if self._oversized:
            return
        if self.transport is None or self.transport.is_closing():
            return

        # RFC 6455 native upgrade (Sec. 4.2), gated on the parser's C-level flag so an
        # ordinary GET never enters the Python header scan. Either way the request must
        # NOT be enqueued: httptools raises `HttpParserUpgrade` for any upgrade, so
        # dispatching would run a handler the client is told failed.
        if self.parser.should_upgrade():
            if not self._handle_websocket_upgrade():
                self._send_bad_request()
            return

        max_len = self.app.config.get("MAX_CONTENT_LENGTH")
        if max_len is not None:
            declared = self._declared_content_length()
            if declared is not None and declared > max_len:
                # Built first, or a refused cross-origin upload reads as a CORS failure.
                self._reject_413(self._refusal_request())
                return

        # RFC 9110 Sec. 10.1.1 forbids the interim to HTTP/1.0, hence the gate.
        if self._wants_continue() and self.transport is not None:
            self.transport.write(b"HTTP/1.1 100 Continue\r\n\r\n")

        keep_alive = self.parser.should_keep_alive()
        # The slowloris guard stays armed: a body still arriving may yet stall.

        path, query_bytes = self._parse_request_target()
        query_string = query_bytes.decode("ascii")

        source = RequestBodySource(max_content_length=max_len)
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
        self._current_source = source
        self._reset_header_state()
        self._headers_done = True
        # Matched ONCE and threaded into `handle_request`: the tree is not walked twice.
        match = self.app.match(request.method, request.path)
        self._request_queue.append((request, source, keep_alive, match))
        # Depth is the only backpressure signal a bodiless request has.
        if len(self._request_queue) >= self._max_pipelined:
            self._pause_reading_depth()
        if self._server_loop is None or self._server_loop.done():
            self._server_loop = self.loop.create_task(self._serve())
            HttpProtocol._active_tasks.add(self._server_loop)
            self._server_loop.add_done_callback(self._task_done)

    # ── WebSocket upgrade (RFC 6455 Sec. 4.2) ─────────────

    def _handle_websocket_upgrade(self) -> bool:
        """Attempt the RFC 6455 handshake; return True if the connection diverted.

        True once the connection has been handled (diverted, or terminated with
        an HTTP error response); False means "not a WebSocket upgrade - fall
        through to the normal HTTP path".
        """
        # RFC 6455 Sec. 4.1 requires a GET, so h2c and the rest fall through to HTTP.
        if self.parser.get_method() != b"GET":
            return False
        is_ws_upgrade, ws_key, ws_version, host, origin = self._scan_ws_headers()
        if not is_ws_upgrade:
            return False

        transport = self.transport
        if transport is None or transport.is_closing():
            return True

        # RFC 6455 Sec. 4.2.2: the wrong version draws a 426 advertising ours.
        if ws_version != _WS_SUPPORTED_VERSION:
            self._write_ws_http_error(
                status.HTTP_426_UPGRADE_REQUIRED,
                b"Upgrade Required",
                extra=b"Sec-WebSocket-Version: 13\r\n",
            )
            return True

        # Without the key (RFC 6455 Sec. 4.2.1) there is no nonce for the 101.
        if ws_key is None:
            self._write_ws_http_error(status.HTTP_400_BAD_REQUEST, b"Bad Request")
            return True

        path, query_bytes = self._parse_request_target()

        # Applied before the route table, because matching first tells an untrusted
        # origin whether a path exists. Pre-101, so the refusal is an HTTP 403.
        host_str = _extract_host(host.decode("latin-1")) if host else ""
        origin_str = origin.decode("latin-1") if origin is not None else ""
        if _ws_handshake_rejection(self.app._middlewares, host_str, origin_str):
            self._write_ws_http_error(status.HTTP_403_FORBIDDEN, b"Forbidden")
            return True

        # Matched BEFORE the 101, so no protocol switch on an unhandled path.
        ws_match = self.app.match(ROUTE_METHOD_WEBSOCKET, path)
        if ws_match is None:
            self._write_ws_http_error(status.HTTP_404_NOT_FOUND, b"Not Found")
            return True

        # Synchronous, so the stream switches (RFC 6455 Sec. 4.2.2) before the next read.
        accept_key = compute_accept(ws_key.decode("latin-1"))
        transport.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept_key.encode("ascii") + b"\r\n\r\n"
        )

        # Mirrors the ASGI websocket scope, so the same accessors work on both.
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
        # Backpressure: a slow reader suspends the handler, not the write buffer.
        ws.set_send_drain(self.drain)
        ws.set_send_gate(self._can_write.is_set)

        self._websocket = ws
        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None
        self._reset_header_state()

        # Held in `_ws_task` so connection_lost can cancel it if the client drops.
        self._ws_task = self.loop.create_task(self.app._run_websocket(ws, ws_match.route_info))
        HttpProtocol._active_tasks.add(self._ws_task)
        self._ws_task.add_done_callback(functools.partial(self._ws_task_done, ws))
        return True

    def _scan_ws_headers(self) -> tuple[bool, bytes | None, bytes | None, bytes, bytes | None]:
        """Read the handshake fields off the parsed headers (RFC 6455 Sec. 4.2.1).

        Returns `(is_ws_upgrade, key, version, host, origin)`, where
        `is_ws_upgrade` is True only when both halves of the upgrade triplet are
        present. `connection` may be a comma list ("keep-alive, Upgrade") while
        the rest are single tokens.
        """
        has_upgrade_token = False
        upgrade_is_ws = False
        ws_key: bytes | None = None
        ws_version: bytes | None = None
        host = b""
        origin: bytes | None = None
        for name, value in self.headers:
            if name == _WS_HEADER_CONNECTION:
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
        return has_upgrade_token and upgrade_is_ws, ws_key, ws_version, host, origin

    def _write_ws_http_error(self, status_code: int, reason: bytes, extra: bytes = b"") -> None:
        """Refuse a handshake with a plain HTTP response, then close.

        The upgrade has not completed on any of the pre-101 refusal paths, so
        the refusal is an ordinary HTTP/1.1 response (RFC 6455 Sec. 4.2.2 /
        Sec. 4.4), never a WebSocket close frame. `extra` carries additional
        header lines, such as the 426's `Sec-WebSocket-Version`.
        """
        self._oversized = True
        self._emit_http_error(status_code, reason, extra=extra)

    def on_body(self, body: bytes) -> None:
        """Feed a body chunk to the in-flight request's source."""
        # The source latches `overflowed` past MAX_CONTENT_LENGTH, so the handler's
        # next read raises 413; the drop keeps an over-reader from streaming on.
        source = self._current_source
        if source is None:
            return
        source.feed(body)
        if source.overflowed:
            self._reject_413()

    def on_message_complete(self) -> None:
        """Close out the message and reopen the header phase for a pipelined one."""
        # The next on_url / on_header belong to a follow-up, so the phase reopens.
        self._headers_done = False
        self._header_bytes_total = 0
        self._header_phase_bytes = 0
        # `None` is what lets a follow-up re-arm it from `data_received`.
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None
        if self._current_source is not None:
            self._current_source.feed_eof()
            self._current_source = None

    # ── asyncio.Protocol callbacks ────────────────────────

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Take the transport, admitting the connection against the concurrency cap."""
        # By capability, not `isinstance(asyncio.Transport)`: uvloop's transport does
        # not subclass it, so an isinstance check fails every production connection.
        if not (
            callable(getattr(transport, "write", None))
            and callable(getattr(transport, "pause_reading", None))
        ):
            raise RuntimeError(
                f"expected a full-duplex transport (write + pause_reading), "
                f"got {type(transport).__name__}"
            )
        transport = cast("asyncio.Transport", transport)
        self.transport = transport

        # Connection cap (DDoS guard), under the lock so parallel `connection_made`
        # calls cannot all see `count == cap - 1` and over-admit. `None`
        # short-circuits because `int >= None` would refuse every connection.
        cap = self.app.config.get("MAX_CONCURRENT_CONNECTIONS", DEFAULT_MAX_CONCURRENT_CONNECTIONS)
        with HttpProtocol._connections_lock:
            if cap is not None and HttpProtocol._active_connections >= cap:
                admitted = False
            else:
                HttpProtocol._active_connections += 1
                admitted = True
                self._counted = True
        if not admitted:
            self._emit_http_error(status.HTTP_503_SERVICE_UNAVAILABLE, b"Service Unavailable")
            return

        # Set explicitly because the proactor/selector defaults can be large.
        high = self.app.config.get("WRITE_BUFFER_HIGH_WATER", WRITE_BUFFER_HIGH_WATER)
        with contextlib.suppress(NotImplementedError, AttributeError):
            transport.set_write_buffer_limits(high=high)

        # Skipped when no settable socket is reachable; duck-typed, for uvloop's.
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

        # Admitted after shutdown began: drain at once, serving one request.
        HttpProtocol._live_connections.add(self)
        if _SHUTTING_DOWN:
            self._draining = True

        self._start_keep_alive_timer()

    def connection_lost(self, exc: Exception | None) -> None:
        """Release everything the connection held and unwind any live handler."""
        if self._counted:
            with HttpProtocol._connections_lock:
                HttpProtocol._active_connections -= 1
            self._counted = False
        HttpProtocol._live_connections.discard(self)
        # Marked closed so a handler blocked in `receive` unwinds on its next read.
        if self._websocket is not None:
            self._websocket._closed = True
            if self._ws_task is not None and not self._ws_task.done():
                self._ws_task.cancel()
            self._websocket = None
            self._ws_task = None
            self.transport = None
            return
        self._closing = True
        # Releases a stream parked in `drain()`, so its next write fails fast.
        self._can_write.set()
        self._request_queue.clear()
        # EOF here, not from the cancellation, is what ends a streaming read cleanly.
        if self._current_source is not None:
            self._current_source.feed_eof(disconnected=True)
            self._current_source = None
        # A streaming response has only the drop to record, for `is_disconnected()`.
        if self._serving_source is not None:
            self._serving_source.mark_disconnected()
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

        The in-flight request runs to completion and the `_serve` loop then
        closes at the request boundary. An idle keep-alive connection is closed
        at once, so it does not linger out its idle timer during shutdown.
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

        Each connection self-quiesces at its own request boundary, so no eager
        per-connection close races a mid-write transport. The live set is
        snapshotted first because `begin_drain` may close (and so deregister) a
        connection, mutating it mid-iteration.
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
        """Start the slowloris read budget as a request's bytes begin arriving.

        The connection is no longer idle, so the keep-alive timer is stood down
        in favour of the shorter request timer.
        """
        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        timeout = self.app.config.get("REQUEST_TIMEOUT", self.REQUEST_TIMEOUT)
        self._request_timer = self.loop.call_later(timeout, self._request_timeout)

    def _set_pause_reason(self, bit: int, paused: bool) -> None:
        """Add or clear one reason to stop reading, pausing the transport once.

        A full request-body buffer and a full pipelining queue both want the
        socket quiet, so the reasons are reference-counted: a resume for one
        cannot cancel the other's pause.
        """
        before = self._pause_reasons
        self._pause_reasons = after = (before | bit) if paused else (before & ~bit)
        if (before == 0) == (after == 0):
            return
        transport = self.transport
        if transport is None or transport.is_closing():
            return
        if after:
            transport.pause_reading()
        else:
            transport.resume_reading()

    def _pause_reading(self) -> None:
        """Stop pulling bytes off the socket - the body buffer is full."""
        self._set_pause_reason(_PAUSE_BODY, True)

    def _resume_reading(self) -> None:
        """Resume pulling bytes once the consumer drains below the low mark."""
        self._set_pause_reason(_PAUSE_BODY, False)

    def _pause_reading_depth(self) -> None:
        """Stop reading - the pipelining queue is at its depth limit."""
        self._set_pause_reason(_PAUSE_DEPTH, True)

    def _resume_reading_depth(self) -> None:
        """Resume reading once the pipelining queue has drained."""
        self._set_pause_reason(_PAUSE_DEPTH, False)

    # ── write-side flow control ───────────────────────────

    def pause_writing(self) -> None:
        """Asyncio callback: the transport write buffer crossed the high mark.

        Clearing the gate makes the next `drain()` block, throttling a producer
        that is outrunning a slow client. Idempotent on purpose: asyncio
        guarantees paired pause/resume, but tolerating a repeat is cheaper than
        asserting on it.
        """
        self._can_write.clear()

    def resume_writing(self) -> None:
        """Asyncio callback: the write buffer drained below the low mark."""
        self._can_write.set()
        # A PONG deferred over the high-water mark goes out now.
        if self._websocket is not None:
            self._websocket.flush_pending_pong()

    async def drain(self) -> None:
        """Block while the transport write buffer is over the high mark.

        Returns immediately on the common path, where the gate is set. A
        closing or absent transport also returns at once, so a torn-down
        connection never parks the producer.
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

        The single framing path for every pre-dispatch refusal. `extra` carries
        additional header lines. This owns only the wire framing: the
        `_oversized` latch, source EOF and timer resets stay in the callers.
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

        For a request line or header block past its cap: the parser cannot be
        trusted to resynchronise, so the connection is terminated.
        """
        self._oversized = True
        self._emit_http_error(status_code, reason)

    def _parse_request_target(self) -> tuple[str, bytes]:
        """Split the parsed request line into `(path, raw_query_bytes)`.

        `path` is percent-decoded as the ASGI scope's `path` is, so one app
        binds `/items/a%20b` the same way however it is served. The decode is
        skipped when the target carries no `%` - almost every request - so the
        common path pays one C-level scan and no allocation. The query is
        returned raw, so the WebSocket scope can carry the bytes verbatim.
        """
        parsed = httptools.parse_url(self.url)
        raw_path = parsed.path
        if not raw_path:
            return "/", parsed.query or b""
        if _PERCENT_BYTE in raw_path:
            # A malformed escape is left as written, as ASGI's `path` holds it.
            return unquote(raw_path.decode("ascii")), parsed.query or b""
        return raw_path.decode("ascii"), parsed.query or b""

    def _declared_content_length(self) -> int | None:
        """Parse the just-parsed request's `Content-Length` header, or None.

        None when absent or malformed, in which case the streamed
        running-total cap is the backstop.
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

        RFC 9110 Sec. 10.1.1 forbids the interim response to an HTTP/1.0
        client, so the version is checked first. Older httptools builds may not
        expose `get_http_version`; its absence means "do not send".
        """
        try:
            if self.parser.get_http_version() != "1.1":
                return False
        except (AttributeError, RuntimeError):
            return False
        return self._has_expect_continue

    def _refusal_request(self) -> Request | None:
        """Build a body-less `Request` for a pre-dispatch refusal, or `None`.

        The request line and headers are already parsed when a declared
        `Content-Length` is checked, so the refusal can carry the same request
        the response phase would have seen. There is deliberately no body
        source: the body is what is being refused.
        """
        transport = self.transport
        if transport is None or transport.is_closing():
            return None
        try:
            path, query_bytes = self._parse_request_target()
            return Request(
                method=self.parser.get_method().decode("ascii"),
                path=path,
                query_string=query_bytes.decode("ascii"),
                headers=list(self.headers),
                body=b"",
                transport=transport,
            )
        except Exception:
            # A refusal never goes unsent: the caller falls back to bare framing.
            return None

    def _reject_413(self, request: Request | None = None) -> None:
        """Emit a 413 and close - the body exceeds MAX_CONTENT_LENGTH.

        The connection is terminated rather than trusted to resynchronise after
        a partially-read over-limit body. With a `request`, the refusal runs
        through the app's response phase so it carries the CORS and security
        headers a served response would; without one there is nothing to run
        middleware against and the bare framing is written directly.
        """
        self._oversized = True
        if self._current_source is not None:
            self._current_source.feed_eof()
            self._current_source = None
        max_size = self.app.config.get("MAX_CONTENT_LENGTH")
        if request is not None:
            cp = self.app._ensure_pipeline()
            # A probe: it separates the refusal written below from one that awaits.
            if (cp is not None and cp.http_post is not None) or self.app._find_error_handler(
                request, body_too_large(max_size)
            ) is not None:
                # A task because the handler may await and this is a sync callback.
                task = self.loop.create_task(self._emit_refusal(request, cp, max_size))
                HttpProtocol._active_tasks.add(task)
                task.add_done_callback(HttpProtocol._active_tasks.discard)
                return
        # The reason phrase is RFC 9110 Sec. 15.5.14's, which the status table still
        # spells the pre-9110 way; the body is the one the ASGI path answers with.
        self._emit_http_error(
            status.HTTP_413_CONTENT_TOO_LARGE,
            b"Content Too Large",
            dumps_for(self.app, too_large_payload(max_size)),
            extra=CONTENT_TYPE_JSON_LINE,
        )

    async def _emit_refusal(
        self, request: Request, cp: CompiledPipeline | None, max_size: int | None
    ) -> None:
        """Build the 413 the way the ASGI path builds it, then frame it and close.

        Through the app's own builder, so a registered `RequestEntityTooLarge`
        handler shapes this refusal exactly as it shapes the ASGI one.
        """
        try:
            response = await self.app._body_too_large_response(request, cp, max_size)
        except Exception:
            # A failing handler must not swallow the refusal.
            _logger.exception("building a body-too-large refusal raised")
            response = Response(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                body=dumps_for(self.app, too_large_payload(max_size)),
                content_type=MIME_JSON,
            )
        transport = self.transport
        if transport is None or transport.is_closing():
            return
        try:
            if request.method == HTTP_METHOD_HEAD:
                # RFC 9110 Sec. 9.3.2: no body whatever the framing says.
                transport.write(_strip_response_body(response.encode(keep_alive=False)))
            elif response.is_streamed:
                await response.stream_to(transport, drain=self.drain, keep_alive=False)
            else:
                transport.write(response.encode(keep_alive=False))
        except Exception:
            # Logged here because this task is never awaited.
            _logger.exception("emitting a body-too-large refusal failed")
        finally:
            # The body was refused unread, so the connection goes regardless.
            transport.close()

    def _request_timeout(self) -> None:
        """Drop a connection whose client took too long to send a full request."""
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
    def _task_done(task: asyncio.Task[Any]) -> None:
        """Log any error from a completed dispatch task and drop the reference."""
        HttpProtocol._active_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _logger.error("Unhandled error in request dispatch: %s", exc, exc_info=exc)

    @staticmethod
    def _ws_task_done(ws: WebSocket, task: asyncio.Task[Any]) -> None:
        """Retire a completed WebSocket dispatch task.

        A handler that raises closes with 1011 first, and that close awaits - so
        a peer already gone lets `connection_lost` cancel this task
        mid-handshake and the exception is lost with it. `_run_websocket`
        records it on the socket beforehand, so a cancellation still reports.
        """
        HttpProtocol._active_tasks.discard(task)
        exc = ws._handler_exc if task.cancelled() else task.exception()
        if exc is not None:
            _logger.error("Unhandled error in websocket handler: %s", exc, exc_info=exc)

    def data_received(self, data: bytes) -> None:
        """Feed inbound bytes to the HTTP parser, or to the frame parser once upgraded."""
        # A close frame raises `WebSocketDisconnect` out of `feed_data`.
        if self._websocket is not None:
            with contextlib.suppress(WebSocketDisconnect):
                self._websocket.feed_data(data)
            return
        if self._oversized:
            return
        if self._request_timer is None:
            self._arm_request_timer()
        self._header_phase_bytes += len(data)
        try:
            self.parser.feed_data(data)
        except httptools.HttpParserUpgrade as upgrade:
            # Not an error: httptools signals the switch `on_headers_complete` made.
            if self._websocket is not None:
                # The offset of the post-handshake body: a client that pipelines its
                # first frame leaves it where the HTTP parser never looks.
                offset = upgrade.args[0]
                if offset < len(data):
                    with contextlib.suppress(WebSocketDisconnect):
                        self._websocket.feed_data(data[offset:])
        except httptools.HttpParserError:
            self._send_bad_request()
            return
        # After the feed: a segment of several requests is not one huge header.
        if not self._headers_done and self._header_phase_bytes > MAX_TOTAL_HEADERS_SIZE:
            self._reject_oversized(
                status.HTTP_431_REQUEST_HEADER_FIELDS_TOO_LARGE,
                b"Request Header Fields Too Large",
            )

    def _send_bad_request(self) -> None:
        """Write a minimal `400 Bad Request` and close the connection."""
        self._emit_http_error(status.HTTP_400_BAD_REQUEST, b"Bad Request", b"Bad Request")

    # ── request dispatch ──────────────────────────────────

    async def _serve(self) -> None:
        """Per-connection server loop: dispatch queued requests one at a time.

        Awaiting each `_dispatch` before pulling the next request is what
        enforces HTTP/1.1 FIFO response ordering, and bounds in-flight work to
        one request per connection.
        """
        while self._request_queue and not self._closing:
            request, source, keep_alive, match = self._request_queue.popleft()
            self._serving_source = source
            # Half the limit, so a steady pipeline does not pause per request.
            if self._pause_reasons & _PAUSE_DEPTH and (
                len(self._request_queue) <= self._max_pipelined >> 1
            ):
                self._resume_reading_depth()
            try:
                should_continue = await self._dispatch(request, source, keep_alive, match)
            finally:
                # In `finally`: a raise would keep the source reachable for life.
                self._serving_source = None
            # A failure in the hook must never break serving.
            hook = HttpProtocol.on_request_complete
            if hook is not None:
                try:
                    hook()
                except Exception:
                    _logger.exception("on_request_complete hook raised")
            if not should_continue:
                # Connection: close (or a failed write) - stop serving.
                return
            # Quiescing: the completed request was honoured, follow-ups declined.
            if self._draining:
                if self.transport is not None and not self.transport.is_closing():
                    self.transport.close()
                return
            # A worker past `max_requests` stops here, not after the whole queue.
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
        # Rearmed so an idle connection is reaped, but skipped while a follow-up is
        # mid-receive: arming both timers could close the transport mid-request.
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
        `source` as the parser delivers it. Once the response is written the
        source is drained to EOF, because a handler that ignored the body must
        not strand unparsed bytes that would corrupt the next pipelined request.

        Returns whether the connection should keep serving subsequent requests.
        """
        timeout = self.app.config.get("REQUEST_HANDLER_TIMEOUT", 30)
        # A non-streaming route wants its body buffered, so the sync `.data` /
        # `.get_json()` / `.form` accessors find it. A `stream=True` route keeps its
        # lazy source: draining one a handler is parked on would make a SECOND
        # waiter on a single-waiter event.
        if match is None or not match.route_info.stream:
            # Usually one segment, so this settles with a read and no coroutine.
            if source.at_eof and not source.total_bytes:
                request._mark_body_buffered()
            else:
                # Re-raising gives a bare 500: the 413 handlers are inside dispatch.
                with contextlib.suppress(RequestEntityTooLarge):
                    await request._drain_body()

        inner = self.loop.create_task(self.app.handle_request(request, match=match))
        detached = False
        try:
            # The shield lets the handler's teardowns finish even when `wait_for`
            # cancels; the explicit task is the handle on one that outlives the wait.
            response = await asyncio.wait_for(asyncio.shield(inner), timeout=timeout)
        except asyncio.CancelledError:
            # The server loop was cancelled: nobody is left to serve.
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
            # The handler outlived the shield: its state is mid-flight, so draining
            # here would race it and the connection is not reused.
            HttpProtocol._active_tasks.add(inner)
            inner.add_done_callback(HttpProtocol._active_tasks.discard)
            inner.add_done_callback(self._on_detached_handler_done(source))
            return self._emit_and_close(response)

        # Discarding an unread body keeps the parser's byte accounting right.
        if not self._closing and not source.at_eof:
            await source.drain()

        if self.transport is None or self.transport.is_closing():
            return False

        # RFC 9110 Sec. 9.3.2: a HEAD response carries the header section a GET would
        # but MUST NOT include a body, which would corrupt keep-alive framing.
        is_head = request.method == HTTP_METHOD_HEAD
        try:
            if response.is_event_source:
                if is_head:
                    self.transport.write(response.encode(keep_alive=False))
                else:
                    await response.stream_to(self.transport, drain=self.drain, keep_alive=False)
                self.transport.close()
                return False
            # `is_streamed`: a class test drops `EventSourceResponse` and a streamed
            # `FileResponse` into the buffered encoder, which cannot emit chunks.
            if response.is_streamed:
                if is_head:
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
        # The parser is NOT recreated: it holds any follow-up's buffered bytes.
        self._reset()
        return True

    def _emit_and_close(self, response: Response) -> bool:
        """Write a plain error response and close the connection.

        For the timeout/error paths where the handler is still detached and
        alive, so the connection cannot be safely reused. Returns False so the
        server loop stops serving.
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
    ) -> Callable[[asyncio.Task[Any]], None]:
        """Build a done-callback that drains the body once a detached handler ends.

        A shielded handler still running may be the source's sole consumer, so
        the body cannot be drained inline; this fires once it completes and no
        consumer is left. The connection is already closing, so this is purely
        buffer cleanup and failures are swallowed.
        """

        def _callback(task: asyncio.Task[Any]) -> None:
            with contextlib.suppress(Exception):
                task.exception()
            # EOF first: `on_message_complete` may never fire on a closing connection.
            source.feed_eof()
            drain_task = self.loop.create_task(source.drain())
            HttpProtocol._active_tasks.add(drain_task)
            drain_task.add_done_callback(HttpProtocol._active_tasks.discard)

        return _callback

    def _reset(self) -> None:
        """Reset per-request scratch state for keep-alive connection reuse.

        Deliberately only `_oversized`. This runs after the prior request's
        dispatch has awaited, by which point the reused parser may already have
        written a follow-up's `on_url` / `on_header` into the live buffers -
        clearing those here would dispatch that follow-up with an empty URL.
        `on_headers_complete` resets them instead, as each request is built.
        """
        self._oversized = False
