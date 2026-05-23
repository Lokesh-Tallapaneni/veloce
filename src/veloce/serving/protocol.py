"""High-performance HTTP/1.1 protocol implementation using httptools."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httptools

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
        "_header_key",
        "_keep_alive",
        "_current_task",
        "_keep_alive_handle",
        "_request_timer",
    )

    # Strong reference set to prevent GC of in-flight tasks
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
        self._header_key: bytes = b""
        self._keep_alive = True
        self._current_task: asyncio.Task | None = None
        self._keep_alive_handle: asyncio.TimerHandle | None = None
        self._request_timer: asyncio.TimerHandle | None = None

    # ── httptools callbacks ──────────────────────────────────────

    def on_url(self, url: bytes) -> None:
        self.url = url

    def on_header(self, name: bytes, value: bytes) -> None:
        self.headers.append((name.lower(), value))

    def on_body(self, body: bytes) -> None:
        self.body_parts.append(body)

    def on_message_complete(self) -> None:
        self.request_complete = True
        # Cancel keep-alive timeout while processing
        if self._keep_alive_handle is not None:
            self._keep_alive_handle.cancel()
            self._keep_alive_handle = None
        # The request finished arriving in time — stand the slowloris
        # guard down.
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None
        # Create task with strong reference to prevent GC and log errors
        task = self.loop.create_task(self._dispatch())
        self._current_task = task
        HttpProtocol._active_tasks.add(task)
        task.add_done_callback(self._task_done)

    # ── asyncio.Protocol callbacks ───────────────────────────────

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # ASGI/HTTP runs over a full duplex transport; the Liskov-correct
        # signature widens to `BaseTransport`, so narrow back here.
        assert isinstance(transport, asyncio.Transport)
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
            import logging

            logging.getLogger("veloce.protocol").error(
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

    async def _dispatch(self) -> None:
        from veloce.http.request import Request
        from veloce.http.response import Response

        method = self.parser.get_method().decode("ascii")
        url_bytes = self.url
        body = b"".join(self.body_parts) if self.body_parts else b""
        headers_dict: dict[str, str] = {}
        for k, v in self.headers:
            headers_dict[k.decode("latin-1")] = v.decode("latin-1")

        # Parse URL
        parsed = httptools.parse_url(url_bytes)
        path = parsed.path.decode("ascii") if parsed.path else "/"
        query_string = parsed.query.decode("ascii") if parsed.query else ""

        request = Request(
            method=method,
            path=path,
            query_string=query_string,
            headers=headers_dict,
            body=body,
            transport=self.transport,
        )

        try:
            response = await self.app.handle_request(request)
        except Exception:
            response = Response(
                status_code=500,
                body=b"Internal Server Error",
                content_type="text/plain",
            )

        if self.transport and not self.transport.is_closing():
            # Handle streaming responses (chunked transfer)
            from veloce.http.response import StreamingResponse
            from veloce.sse import EventSourceResponse

            if isinstance(response, (StreamingResponse, EventSourceResponse)):
                await response.stream_to(self.transport)
                self.transport.close()  # Streaming responses close after completion
            else:
                self.transport.write(response.encode())
                if not self._keep_alive or headers_dict.get("connection", "").lower() == "close":
                    self.transport.close()
                else:
                    self._reset()

    def _reset(self) -> None:
        """Reset state for keep-alive connection reuse."""
        self.parser = httptools.HttpRequestParser(self)
        self.url = b""
        self.headers = []
        self.body_parts = []
        self.request_complete = False
        self._current_task = None
        self._start_keep_alive_timer()
