"""High-performance HTTP/1.1 protocol implementation using httptools."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from typing import TYPE_CHECKING

import httptools

from veloce import status
from veloce.http._body import RequestBodySource
from veloce.http.request import Request
from veloce.http.response import Response, StreamingResponse

if TYPE_CHECKING:
    from veloce.app import Veloce


# Per-field + cumulative caps on the request line and headers. Prevents a
# malicious client from streaming megabytes of headers and pinning RAM
# before the body limit (MAX_CONTENT_LENGTH) gets a chance to engage.
MAX_URL_SIZE = 8192
MAX_HEADER_SIZE = 8192
MAX_TOTAL_HEADERS_SIZE = 65536

# Per-process cap on simultaneously-open connections. Without it, a DDoS
# can exhaust RAM by opening sockets faster than dispatch can drain them.
DEFAULT_MAX_CONCURRENT_CONNECTIONS = 1000


class HttpProtocol(asyncio.Protocol):
    """Raw asyncio protocol — bypasses ASGI overhead entirely."""

    __slots__ = (
        "app",
        "loop",
        "transport",
        "parser",
        "url",
        "headers",
        "request_complete",
        "_keep_alive_handle",
        "_request_timer",
        "_body_size",
        "_header_bytes_total",
        "_oversized",
        "_counted",
        "_request_queue",
        "_server_loop",
        "_closing",
        "_current_source",
    )

    # Class-level set: prevents GC of in-flight tasks across all connections.
    _active_tasks: set[asyncio.Task] = set()

    # Cross-thread connection counter. `+=` on a class-level int is read-
    # modify-write, not atomic under the GIL, so guard with a Lock. Only
    # contended on connection setup/teardown — not on the per-request path.
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
        self.request_complete = False
        self._keep_alive_handle: asyncio.TimerHandle | None = None
        self._request_timer: asyncio.TimerHandle | None = None
        self._body_size: int = 0
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

    # ── httptools callbacks ──────────────────────────────────────

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
        self.headers.append((name.lower(), value))

    def on_headers_complete(self) -> None:
        """Headers are fully parsed — build the Request and dispatch it now.

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
        # byte — cheapest possible 413 for an honest client that announces an
        # over-limit upload up front.
        if max_len is not None:
            declared = self._declared_content_length()
            if declared is not None and declared > max_len:
                self._reject_413()
                return

        keep_alive = self.parser.should_keep_alive()
        # The slowloris guard stays armed: the body may still be arriving, and
        # a stalled body must still time out. It is stood down only at
        # message-complete. The idle keep-alive timer was already cancelled
        # when the first bytes arrived (`_arm_request_timer`).

        parsed = httptools.parse_url(self.url)
        path = parsed.path.decode("ascii") if parsed.path else "/"
        query_string = parsed.query.decode("ascii") if parsed.query else ""

        source = RequestBodySource(max_content_length=max_len)
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
        # header buffers must be cleared now — a follow-up request's on_url /
        # on_header would otherwise append into the same live lists. The
        # already-built Request holds its own copies.
        self._current_source = source
        self.url = b""
        self.headers = []
        self._body_size = 0
        self._header_bytes_total = 0
        self._request_queue.append((request, source, keep_alive))
        # Start the per-connection server loop on the first queued request; it
        # runs until the queue drains, guaranteeing FIFO response ordering.
        if self._server_loop is None or self._server_loop.done():
            self._server_loop = self.loop.create_task(self._serve())
            HttpProtocol._active_tasks.add(self._server_loop)
            self._server_loop.add_done_callback(self._task_done)

    def on_body(self, body: bytes) -> None:
        # Feed the in-flight request's body source. The source tracks the
        # running total and flips an overflow latch past MAX_CONTENT_LENGTH;
        # the handler's next read then raises 413. We also short-circuit the
        # transport here so an over-reading client is dropped promptly rather
        # than allowed to keep streaming megabytes we will only discard.
        source = self._current_source
        if source is None:
            return
        self._body_size += len(body)
        source.feed(body)
        max_len = self.app.config.get("MAX_CONTENT_LENGTH")
        if max_len is not None and self._body_size > max_len:
            self._reject_413()

    def on_message_complete(self) -> None:
        # The body finished arriving in time — stand the slowloris guard down
        # and signal EOF to the in-flight source so a streaming consumer ends.
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None
        # This request is fully received; a pipelined follow-up's first bytes
        # should re-arm the slowloris timer, so clear the mid-request flag.
        self.request_complete = False
        if self._current_source is not None:
            self._current_source.feed_eof()
            self._current_source = None

    # ── asyncio.Protocol callbacks ───────────────────────────────

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

        self._start_keep_alive_timer()

    def connection_lost(self, exc: Exception | None) -> None:
        if self._counted:
            with HttpProtocol._connections_lock:
                HttpProtocol._active_connections -= 1
            self._counted = False
        # Stop the server loop from dispatching further queued requests and
        # drop any not-yet-served pipelined work; the client is gone.
        self._closing = True
        self._request_queue.clear()
        # Unblock any consumer awaiting more body on the in-flight source —
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
        self._request_timer = self.loop.call_later(self.REQUEST_TIMEOUT, self._request_timeout)

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

        Read at headers-complete, off the raw `(name, value)` tuples the
        parser appended, so an over-limit upload can be refused before its
        body is read. Returns None when absent or malformed (the streamed
        running-total cap is the backstop in that case).
        """
        for name, value in self.headers:
            if name == b"content-length":
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return None
        return None

    def _reject_413(self) -> None:
        """Emit a 413 and close — the body exceeds MAX_CONTENT_LENGTH.

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
        """A client took too long to send a complete request — drop it."""
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
        """Start idle timeout — close connection if no request arrives."""
        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
        self._keep_alive_handle = self.loop.call_later(
            self.KEEP_ALIVE_TIMEOUT, self._keep_alive_timeout
        )

    def _keep_alive_timeout(self) -> None:
        """Close idle connection after timeout."""
        if self.transport and not self.transport.is_closing():
            self.transport.close()

    @staticmethod
    def _task_done(task: asyncio.Task) -> None:
        """Callback for completed dispatch tasks — log errors, remove reference."""
        HttpProtocol._active_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logging.getLogger("veloce.serving.protocol").error(
                "Unhandled error in request dispatch: %s", exc, exc_info=exc
            )

    def data_received(self, data: bytes) -> None:
        if self._oversized:
            return
        # First bytes of a fresh request — arm the slowloris read budget.
        if self._request_timer is None and not self.request_complete:
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

    # ── request dispatch ─────────────────────────────────────────

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
            if not should_continue:
                # Connection: close (or a failed write) — stop serving.
                return
        # Queue drained on a keep-alive connection: rearm the idle timer so the
        # connection is reaped if no further request arrives. Skip the rearm if a
        # follow-up is mid-receive (_request_timer live) or already queued — the
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
        response is written) the source is drained to EOF — a handler that
        ignored the body must not strand unparsed bytes that would corrupt the
        next pipelined request or wedge the connection.

        Returns whether the connection should keep serving subsequent requests
        (True for keep-alive, False when the connection was or must be closed).
        """
        timeout = self.app.config.get("REQUEST_HANDLER_TIMEOUT", 30)
        try:
            try:
                # `asyncio.shield` lets the handler's finally-block teardowns
                # (yield-dep cleanup, teardown_request hooks) run to completion
                # even when wait_for cancels on timeout. Without it,
                # CancelledError propagates into async teardowns and can
                # interrupt resource cleanup (DB connections, file handles).
                response = await asyncio.wait_for(
                    asyncio.shield(self.app.handle_request(request)), timeout=timeout
                )
            except asyncio.TimeoutError:
                response = Response(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    body=b"Gateway Timeout",
                    content_type="text/plain",
                )
            except Exception:
                logging.getLogger("veloce.serving.protocol").exception(
                    "Unhandled exception in request dispatch"
                )
                response = Response(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    body=b"Internal Server Error",
                    content_type="text/plain",
                )
        finally:
            # Mandatory drain-on-teardown: consume any body the handler left
            # unread so the parser's body bytes for this request are fully
            # accounted for before the next pipelined request is pulled. EOF
            # arrives via on_message_complete; if the connection is already
            # tearing down, the source was fed EOF in connection_lost / 413.
            if not self._closing:
                await source.drain()

        if self.transport is None or self.transport.is_closing():
            return False

        try:
            if getattr(response, "is_event_source", False):
                await response.stream_to(self.transport)  # type: ignore[attr-defined]
                self.transport.close()
                return False
            if isinstance(response, StreamingResponse):
                await response.stream_to(self.transport)
            else:
                self.transport.write(response.encode())
        except Exception:
            logging.getLogger("veloce.serving.protocol").exception("Error during response emission")
            self.transport.close()
            return False

        if not keep_alive:
            self.transport.close()
            return False
        # Keep-alive: keep serving. The parser is intentionally NOT recreated —
        # it still holds buffered bytes for any pipelined follow-up request.
        self._reset()
        return True

    def _reset(self) -> None:
        """Reset per-request scratch state for keep-alive connection reuse.

        Only state that cannot already belong to a started pipelined follow-up
        is cleared here. The URL / header buffers and their size counters are
        reset by on_headers_complete the moment a request is built — and a
        reused httptools parser may have already written a follow-up's on_url /
        on_header into those same live buffers by the time this runs (after the
        prior request's dispatch awaits). Clearing them here would destroy that
        follow-up's parse state and dispatch it with an empty URL, so they are
        left alone. Only _oversized (a per-connection reject latch that no
        normal follow-up sets) is cleared. The idle keep-alive timer is rearmed
        by the server loop once the request queue drains, not per request.
        """
        self._oversized = False
