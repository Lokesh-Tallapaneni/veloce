"""Serving protocol - high-performance HTTP/1.1 over a raw asyncio transport.

Parses the wire with httptools and dispatches requests through the Veloce app,
bypassing the ASGI layer entirely.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

import httptools

from veloce import status
from veloce._constants import (
    MIME_TEXT_PLAIN,
    MSG_ERROR_RESPONSE_EMISSION,
    MSG_INTERNAL_SERVER_ERROR,
)
from veloce._protocol_constants import RAW_HEADER_CONTENT_LENGTH
from veloce.http._body import RequestBodySource
from veloce.http.request import Request
from veloce.http.response import Response, StreamingResponse

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce

_logger = logging.getLogger(__name__)


# Per-field + cumulative caps on the request line and headers. Prevents a
# malicious client from streaming megabytes of headers and pinning RAM
# before the body limit (MAX_CONTENT_LENGTH) gets a chance to engage.
MAX_URL_SIZE = 8192
MAX_HEADER_SIZE = 8192
MAX_TOTAL_HEADERS_SIZE = 65536

# Per-process cap on simultaneously-open connections. Without it, a DDoS
# can exhaust RAM by opening sockets faster than dispatch can drain them.
DEFAULT_MAX_CONCURRENT_CONNECTIONS = 1000

# Write-side flow-control watermarks (bytes). When a streaming/SSE producer
# outruns a slow client the event loop's transport write buffer grows; left
# unbounded that is a per-connection memory-exhaustion vector. Handing these
# to `transport.set_write_buffer_limits()` makes asyncio invoke
# `pause_writing`/`resume_writing` once the buffer crosses the high/low mark,
# which the streaming path awaits on. The low mark is left to asyncio's
# default (a quarter of high) when only the high mark is supplied.
WRITE_BUFFER_HIGH_WATER = 256 * 1024


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
        "_oversized",
        "_counted",
        "_request_queue",
        "_server_loop",
        "_closing",
        "_current_source",
        "_raw_content_length",
        "_has_expect_continue",
        "_can_write",
    )

    # Class-level set: prevents GC of in-flight tasks across all connections.
    _active_tasks: set[asyncio.Task] = set()

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

    KEEP_ALIVE_TIMEOUT = 75  # seconds (matches nginx default)
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
        self._request_queue: deque[tuple[Request, RequestBodySource, bool]] = deque()
        self._server_loop: asyncio.Task | None = None
        # Set on teardown so an in-flight server loop stops pulling more work
        # and a client that closes mid-pipeline does not wedge the loop.
        self._closing: bool = False
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
        # streaming/SSE path awaits `drain()`, which only blocks while cleared,
        # so the common keep-alive path pays a single already-set `Event` check.
        self._can_write: asyncio.Event = asyncio.Event()
        self._can_write.set()

    # -- httptools callbacks --------------------------------------

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

        parsed = httptools.parse_url(self.url)
        path = parsed.path.decode("ascii") if parsed.path else "/"
        query_string = parsed.query.decode("ascii") if parsed.query else ""

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
        self._raw_content_length = None
        self._has_expect_continue = False
        self._request_queue.append((request, source, keep_alive))
        # Start the per-connection server loop on the first queued request; it
        # runs until the queue drains, guaranteeing FIFO response ordering.
        if self._server_loop is None or self._server_loop.done():
            self._server_loop = self.loop.create_task(self._serve())
            HttpProtocol._active_tasks.add(self._server_loop)
            self._server_loop.add_done_callback(self._task_done)

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

    # -- asyncio.Protocol callbacks -------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # ASGI/HTTP runs over a full duplex transport; the Liskov-correct
        # signature widens to `BaseTransport`, so narrow back here.
        # Explicit raise (not `assert`) so `python -O` does not strip the
        # check and let a half-duplex transport flow into code that
        # assumes full-duplex semantics two frames deeper.
        if not isinstance(transport, asyncio.Transport):
            raise RuntimeError(
                f"expected a full-duplex asyncio.Transport, got {type(transport).__name__}"
            )
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
            if not transport.is_closing():
                transport.write(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
                transport.close()
            return

        # Arm write-side flow control: asyncio fires `pause_writing` once the
        # buffer exceeds the high mark and `resume_writing` when it drains below
        # the low mark, which the streaming path awaits via `drain()`. Without a
        # set high mark the proactor/selector defaults can be large, so set it
        # explicitly. Wrapped because some transports do not implement it.
        high = self.app.config.get("WRITE_BUFFER_HIGH_WATER", WRITE_BUFFER_HIGH_WATER)
        with contextlib.suppress(NotImplementedError, AttributeError):
            transport.set_write_buffer_limits(high=high)

        self._start_keep_alive_timer()

    def connection_lost(self, exc: Exception | None) -> None:
        if self._counted:
            with HttpProtocol._connections_lock:
                HttpProtocol._active_connections -= 1
            self._counted = False
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

    # -- write-side flow control ----------------------------------

    def pause_writing(self) -> None:
        """asyncio callback: the transport write buffer crossed the high mark.

        Clearing the gate makes the next `drain()` block, throttling a
        producer that is outrunning a slow client. Idempotent - asyncio
        guarantees paired pause/resume, but tolerating a repeat avoids the
        crash-on-double-pause assert aiohttp carries.
        """
        self._can_write.clear()

    def resume_writing(self) -> None:
        """asyncio callback: the write buffer drained below the low mark."""
        self._can_write.set()

    async def drain(self) -> None:
        """Block while the transport write buffer is over the high mark.

        Returns immediately on the common path (gate set). The streaming and
        SSE response paths await this after writing each chunk so a fast
        producer cannot grow the event loop's write buffer without bound. A
        closing/absent transport returns at once so a torn-down connection
        does not park the producer.
        """
        if self._can_write.is_set():
            return
        transport = self.transport
        if transport is None or transport.is_closing():
            return
        await self._can_write.wait()

    def _reject_oversized(self, status_code: int, reason: bytes) -> None:
        """Emit a minimal HTTP/1.1 error response and close the connection.

        Used when the request line or headers exceed configured caps; we
        can't trust the parser to recover, so the connection is terminated.
        """
        self._oversized = True
        if self.transport is None or self.transport.is_closing():
            return
        phrase = reason.decode("ascii")
        head = (
            f"HTTP/1.1 {status_code} {phrase}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        self.transport.write(head)
        self.transport.close()

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
        if self.transport is not None and not self.transport.is_closing():
            self.transport.write(
                b"HTTP/1.1 413 Content Too Large\r\n"
                b"Content-Length: 17\r\n"
                b"Connection: close\r\n\r\n"
                b"Content Too Large"
            )
            self.transport.close()

    def _request_timeout(self) -> None:
        """A client took too long to send a complete request - drop it."""
        self._request_timer = None
        if self.transport and not self.transport.is_closing():
            self.transport.write(
                b"HTTP/1.1 408 Request Timeout\r\n"
                b"Content-Length: 15\r\n"
                b"Connection: close\r\n\r\n"
                b"Request Timeout"
            )
            self.transport.close()

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

    def data_received(self, data: bytes) -> None:
        if self._oversized:
            return
        # First bytes of a fresh request - arm the slowloris read budget. The
        # timer is None between requests (cancelled at on_message_complete), so
        # a pipelined follow-up's first bytes re-arm it here.
        if self._request_timer is None:
            self._arm_request_timer()
        try:
            self.parser.feed_data(data)
        except httptools.HttpParserError:
            if self.transport:
                self.transport.write(
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Content-Length: 11\r\n"
                    b"Connection: close\r\n\r\n"
                    b"Bad Request"
                )
                self.transport.close()

    # -- request dispatch -----------------------------------------

    async def _serve(self) -> None:
        """Per-connection server loop: dispatch queued requests one at a time.

        Awaiting each `_dispatch` to completion before pulling the next request
        enforces HTTP/1.1 FIFO response ordering and bounds in-flight work to a
        single request per connection. The loop exits when the queue drains or
        the connection is being torn down.
        """
        while self._request_queue and not self._closing:
            request, source, keep_alive = self._request_queue.popleft()
            should_continue = await self._dispatch(request, source, keep_alive)
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
        inner = self.loop.create_task(self.app.handle_request(request))
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
        # source was already fed EOF in connection_lost / 413.
        if not self._closing:
            await source.drain()

        if self.transport is None or self.transport.is_closing():
            return False

        try:
            if getattr(response, "is_event_source", False):
                await response.stream_to(self.transport, drain=self.drain)  # type: ignore[attr-defined]
                self.transport.close()
                return False
            if isinstance(response, StreamingResponse):
                await response.stream_to(self.transport, drain=self.drain)
            else:
                self.transport.write(response.encode())
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
                transport.write(response.encode())
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
