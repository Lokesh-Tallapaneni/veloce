"""High-performance HTTP/1.1 protocol implementation using httptools."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httptools

from veloce import status
from veloce.http.request import Request
from veloce.http.response import Response, StreamingResponse

if TYPE_CHECKING:
    from veloce.app import Veloce


class HttpProtocol(asyncio.Protocol):
    """Raw asyncio protocol — bypasses ASGI overhead entirely."""

    __slots__ = (
        "app",
        "loop",
        "transport",
        "parser",
        "url",
        "headers",
        "body_parts",
        "request_complete",
        "_keep_alive",
        "_keep_alive_handle",
        "_request_timer",
        "_body_size",
    )

    # Class-level set: prevents GC of in-flight tasks across all connections.
    _active_tasks: set[asyncio.Task] = set()

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
        self.body_parts: list[bytes] = []
        self.request_complete = False
        self._keep_alive = True
        self._keep_alive_handle: asyncio.TimerHandle | None = None
        self._request_timer: asyncio.TimerHandle | None = None
        self._body_size: int = 0

    # ── httptools callbacks ──────────────────────────────────────

    def on_url(self, url: bytes) -> None:
        self.url = url

    def on_header(self, name: bytes, value: bytes) -> None:
        self.headers.append((name.lower(), value))

    def on_body(self, body: bytes) -> None:
        self._body_size += len(body)
        max_len = self.app.config.get("MAX_CONTENT_LENGTH")
        if max_len is not None and self._body_size > max_len:
            if self.transport and not self.transport.is_closing():
                self.transport.write(
                    b"HTTP/1.1 413 Content Too Large\r\n"
                    b"Content-Length: 17\r\n"
                    b"Connection: close\r\n\r\n"
                    b"Content Too Large"
                )
                self.transport.close()
            return
        self.body_parts.append(body)

    def on_message_complete(self) -> None:
        # F2: Don't dispatch if the connection was already closed (e.g. 413 rejection).
        if self.transport is None or self.transport.is_closing():
            return

        self.request_complete = True
        self._keep_alive = self.parser.should_keep_alive()
        # Cancel keep-alive timeout while processing
        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        # The request finished arriving in time — stand the slowloris
        # guard down.
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None
        # Snapshot mutable request state before scheduling so a pipelined
        # follow-up request cannot overwrite the URL/headers/body that
        # _dispatch will read.
        snap_url = self.url
        snap_headers = self.headers
        snap_body = self.body_parts
        self.url = b""
        self.headers = []
        self.body_parts = []
        self._body_size = 0
        # Create task with strong reference to prevent GC and log errors
        task = self.loop.create_task(self._dispatch(snap_url, snap_headers, snap_body))
        HttpProtocol._active_tasks.add(task)
        task.add_done_callback(self._task_done)

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
        self._start_keep_alive_timer()

    def connection_lost(self, exc: Exception | None) -> None:
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

    async def _dispatch(
        self,
        url: bytes,
        headers: list[tuple[bytes, bytes]],
        body_parts: list[bytes],
    ) -> None:
        method = self.parser.get_method().decode("ascii")
        body = b"".join(body_parts) if body_parts else b""

        parsed = httptools.parse_url(url)
        path = parsed.path.decode("ascii") if parsed.path else "/"
        query_string = parsed.query.decode("ascii") if parsed.query else ""

        request = Request(
            method=method,
            path=path,
            query_string=query_string,
            headers=headers,
            body=body,
            transport=self.transport,
        )

        timeout = self.app.config.get('REQUEST_HANDLER_TIMEOUT', 30)
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
            logging.getLogger("veloce.serving.protocol").exception("Unhandled exception in request dispatch")
            response = Response(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                body=b"Internal Server Error",
                content_type="text/plain",
            )

        if self.transport and not self.transport.is_closing():
            try:
                if getattr(response, "is_event_source", False):
                    await response.stream_to(self.transport)
                    self.transport.close()
                elif isinstance(response, StreamingResponse):
                    await response.stream_to(self.transport)
                    if not self._keep_alive:
                        self.transport.close()
                    else:
                        self._reset()
                else:
                    self.transport.write(response.encode())
                    if not self._keep_alive:
                        self.transport.close()
                    else:
                        self._reset()
            except Exception:
                logging.getLogger("veloce.serving.protocol").exception("Error during response emission")
                self.transport.close()

    def _reset(self) -> None:
        """Reset state for keep-alive connection reuse."""
        self.parser = httptools.HttpRequestParser(self)
        self.url = b""
        self.headers = []
        self.body_parts = []
        self.request_complete = False
        self._body_size = 0
        self._start_keep_alive_timer()
