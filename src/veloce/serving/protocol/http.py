"""HttpProtocol — HTTP/1.1 over a raw asyncio transport, parsed with httptools."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket as _socket
import threading
import weakref
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, cast

import httptools

import veloce.status as status
from veloce._constants import (
    MIME_TEXT_PLAIN,
    MSG_ERROR_RESPONSE_EMISSION,
    MSG_INTERNAL_SERVER_ERROR,
)
from veloce._protocol_constants import (
    HTTP_METHOD_HEAD,
    RAW_HEADER_CONTENT_LENGTH,
)
from veloce.config import (
    DEFAULT_MAX_CONCURRENT_CONNECTIONS,
)
from veloce.config import Config as _Config
from veloce.exceptions import RequestEntityTooLarge, WebSocketDisconnect
from veloce.http._body import RequestBodySource
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.websocket import WebSocket

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce
    from veloce.routing.router import RouteMatch
from veloce.serving.protocol._constants import (
    _PAUSE_BODY,
    _PAUSE_DEPTH,
    MAX_HEADER_SIZE,
    MAX_TOTAL_HEADERS_SIZE,
    MAX_URL_SIZE,
    WRITE_BUFFER_HIGH_WATER,
)
from veloce.serving.protocol._helpers import (
    _enable_tcp_keepalive,
    _KeepServingPredicate,
    _RequestCompleteHook,
    _strip_response_body,
)
from veloce.serving.protocol._refusal import _RefusalMixin
from veloce.serving.protocol._websocket import _WsUpgradeMixin

_logger = logging.getLogger(__name__)


# Graceful-shutdown latch: a connection admitted after it is set drains at once.
_SHUTTING_DOWN = False


class HttpProtocol(_WsUpgradeMixin, _RefusalMixin, asyncio.Protocol):
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

    # Walked by graceful shutdown; weak, so a torn-down connection drops itself.
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
        # Dropped pre-capture, so a trailer named `content-length` cannot poison
        # the early-413 guard (RFC 9112 Sec. 7.1.2).
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
        otherwise append into the live lists. `_header_bytes_total` is re-zeroed
        by `on_message_complete` instead, because chunked trailers keep charging
        that budget after this has run.
        """
        self.url = b""
        self.headers = []
        self._header_bytes_total = 0
        self._header_phase_bytes = 0
        self._raw_content_length = None
        self._has_expect_continue = False

    def on_headers_complete(self) -> None:
        """Headers are fully parsed - build the Request and dispatch it now.

        Enqueued before the body arrives, so a streaming handler sees chunks as
        the socket delivers them.
        """
        if self._oversized:
            return
        if self.transport is None or self.transport.is_closing():
            return

        # RFC 6455 Sec. 4.2, gated on the parser's C-level flag so an ordinary GET
        # never enters the Python scan. Never enqueued: httptools always raises.
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

    def on_body(self, body: bytes) -> None:
        """Feed a body chunk to the in-flight request's source."""
        # The source latches `overflowed`, so the handler's next read raises 413.
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
        # By capability: uvloop's transport does not subclass `asyncio.Transport`.
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

        # Under the lock, or parallel admits all see `count == cap - 1`.
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

        The in-flight request completes and `_serve` closes at the boundary; an
        idle keep-alive connection closes at once rather than out its timer.
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

        Each self-quiesces at its own request boundary, so no eager close races
        a mid-write transport. The live set is snapshotted first because
        `begin_drain` may deregister a connection, mutating it mid-iteration.
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

        A full body buffer and a full pipelining queue both want the socket
        quiet, so a resume for one cannot cancel the other's pause.
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
        outrunning a slow client. Idempotent, since tolerating a repeated
        pause/resume is cheaper than asserting on the pairing.
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

        Returns at once on the common path and on a closing or absent transport,
        so a torn-down connection never parks the producer.
        """
        if self._can_write.is_set():
            return
        transport = self.transport
        if transport is None or transport.is_closing():
            return
        await self._can_write.wait()

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

        A handler that raises closes with 1011 first, and that close awaits, so
        a peer already gone lets `connection_lost` cancel this task and lose the
        exception. `_run_websocket` records it on the socket beforehand.
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
                # A client pipelining its first frame leaves it past this offset.
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
        enforces HTTP/1.1 FIFO response ordering.
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
        # Skipped mid-receive: both timers armed could close the transport.
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

        The body streams into `source` as the parser delivers it, and is drained
        to EOF once the response is written: a handler that ignored the body must
        not strand bytes that would corrupt the next pipelined request. Returns
        whether the connection should keep serving.
        """
        timeout = self.app.config.get("REQUEST_HANDLER_TIMEOUT", 30)
        # Buffered for the sync `.data` / `.get_json()` / `.form` accessors; a
        # `stream=True` source stays lazy, or a parked handler gets a 2nd waiter.
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
            # The shield lets teardowns finish when `wait_for` cancels; the task is
            # the handle on a handler that outlives the wait.
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
            # The handler outlived the shield, so draining here would race it.
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
            # Not a class test: that drops a streamed `FileResponse` into the
            # buffered encoder, which cannot emit chunks.
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
        alive, so the connection cannot be reused. Returns False to stop serving.
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
        the body cannot be drained inline. The connection is already closing, so
        this is buffer cleanup and failures are swallowed.
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

        Deliberately only `_oversized`: this runs after the prior dispatch has
        awaited, by which point the reused parser may already have written a
        follow-up's `on_url` / `on_header` into the live buffers, so clearing
        those here would dispatch it with an empty URL.
        """
        self._oversized = False
