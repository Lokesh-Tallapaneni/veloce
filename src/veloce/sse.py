"""Server-Sent Events (SSE) — streaming event responses."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

from veloce._constants import (
    HEADER_CACHE_CONTROL,
    HEADER_CONTENT_TYPE,
    HEADER_TRANSFER_ENCODING,
    HEADER_VALUE_CHUNKED,
    HEADER_VALUE_NO_CACHE,
    HEADER_X_ACCEL_BUFFERING,
    MIME_TEXT_EVENT_STREAM,
)
from veloce._internal import _write_chunked, dumps_current
from veloce.http.response import Response
from veloce.status import HTTP_200_OK

# A colon-prefixed comment line the spec requires clients to ignore, sent so an
# idle connection survives proxy and load-balancer read timeouts.
_PING_FRAME = b": ping\r\n\r\n"

# What an SSE source iterator may yield; `_encode_event` has the accepted forms.
_SSEItem = Any


class ServerSentEvent:
    """A single SSE event."""

    __slots__ = ("comment", "data", "event", "id", "retry")

    def __init__(
        self,
        data: str | None = None,
        event: str | None = None,
        id: str | None = None,
        retry: int | None = None,
        comment: str | None = None,
    ) -> None:
        # WHATWG SSE: `event` and `id` are single-line fields, and a NUL in `id`
        # makes the client ignore it, breaking Last-Event-ID reconnection.
        # `data` stays permissive, and may be None for a comment-only event.
        if event is not None:
            event = str(event)
            if "\n" in event or "\r" in event:
                raise ValueError("SSE event field must not contain a newline")
        if id is not None:
            id = str(id)
            if "\n" in id or "\r" in id or "\x00" in id:
                raise ValueError("SSE id field must not contain a newline or NUL byte")
        self.data = data
        self.event = event
        self.id = id
        self.retry = retry
        # WHATWG SSE: a `:`-prefixed line is a comment, line-split like `data`,
        # so the field stays permissive.
        self.comment = comment

    @classmethod
    def json(
        cls,
        payload: Any,
        *,
        event: str | None = None,
        id: str | None = None,
        retry: int | None = None,
    ) -> ServerSentEvent:
        """Build an event whose `data` field is `payload` serialized to JSON.

        Use the regular constructor when the payload is already a formatted
        string.
        """
        return cls(
            # The shared encoder, so a streamed event carries the same JSON
            # dialect the application's responses do.
            data=dumps_current(payload).decode("utf-8"),
            event=event,
            id=id,
            retry=retry,
        )

    def encode(self) -> bytes:
        """Encode the event as an SSE-formatted byte string."""
        lines = []
        if self.comment is not None:
            # Line-split like `data`, so an embedded newline emits one `: ` line
            # per segment instead of a mangled single line.
            c = self.comment.replace("\r\n", "\n").replace("\r", "\n")
            if "\n" not in c:
                lines.append(f": {c}")
            else:
                for line in c.split("\n"):
                    lines.append(f": {line}")
        if self.id is not None:
            # `id`/`event` were validated single-line at construction, so emit
            # them directly without a per-encode strip.
            lines.append(f"id: {self.id}")
        if self.event is not None:
            lines.append(f"event: {self.event}")
        if self.retry is not None:
            lines.append(f"retry: {self.retry}")
        if self.data is not None:
            data = self.data.replace("\r\n", "\n").replace("\r", "\n")
            # Single-line payloads - by far the common case - skip the
            # `split("\n")` allocation and emit the field directly.
            if "\n" not in data:
                lines.append(f"data: {data}")
            else:
                for line in data.split("\n"):
                    lines.append(f"data: {line}")
        lines.append("")
        lines.append("")
        return "\n".join(lines).encode("utf-8")


class EventSourceResponse(Response):
    """SSE streaming response - sends events over a long-lived connection.

    Usage::

        @app.get("/events")
        async def events(request: Request):
            async def generate():
                for i in range(10):
                    yield ServerSentEvent(data=f"Event {i}")
                    await asyncio.sleep(1)
            return EventSourceResponse(generate())

    Pass `ping=<seconds>` to emit a keep-alive comment frame whenever no
    event is produced within that interval - useful for holding idle
    connections open through proxies that close silent sockets.
    """

    is_event_source = True

    __slots__ = ("_ping_frame", "ping")

    def __init__(
        self,
        content: AsyncIterator[_SSEItem],
        status_code: int = HTTP_200_OK,
        headers: dict[str, str] | None = None,
        ping: float | None = None,
        ping_comment: str | None = None,
    ) -> None:
        if ping is not None and not (math.isfinite(ping) and ping > 0):
            # `not finite` rejects NaN, which `<= 0` lets slip through, and
            # Infinity, as whose timeout the heartbeat would never fire.
            raise ValueError(
                f"ping interval must be a finite positive number of seconds, got {ping!r}"
            )
        if ping_comment is not None:
            if ping is None:
                raise ValueError("ping_comment is only meaningful when ping is set")
            # Reuse the event-field single-line rule: the keep-alive frame is a
            # single comment line, so a CR/LF would split it on the wire.
            if "\n" in ping_comment or "\r" in ping_comment:
                raise ValueError("ping_comment must not contain a newline")
            # Built directly rather than via `ServerSentEvent.encode` to keep the
            # CRLF framing `_PING_FRAME` uses.
            self._ping_frame = f": {ping_comment}\r\n\r\n".encode()
        else:
            self._ping_frame = _PING_FRAME
        hdrs = dict(headers) if headers else {}
        # `Connection` is deliberately not set: it is the transport's decision,
        # which `encode()` takes.
        hdrs.update(
            {
                HEADER_CACHE_CONTROL: HEADER_VALUE_NO_CACHE,
                HEADER_X_ACCEL_BUFFERING: "no",
            }
        )
        super().__init__(
            status_code=status_code,
            body=b"",
            content_type=MIME_TEXT_EVENT_STREAM,
            headers=hdrs,
        )
        self.ping = ping
        # Normalised to bytes up front, so both transports consume one stream.
        self._stream = self._encode_stream(content)

    def encode(self, keep_alive: bool = True) -> bytes:
        """Encode the event-stream head.

        The native transport calls `encode()` to answer a HEAD, and RFC 9110
        Sec. 9.3.2 requires the header section a GET would have produced - not
        the `Content-Length: 0` a buffered `Response.encode()` would advertise.
        `stream_to` writes this same head.
        """
        return self._encode_streaming_head(
            {
                HEADER_CONTENT_TYPE: self.content_type,
                HEADER_CACHE_CONTROL: HEADER_VALUE_NO_CACHE,
                HEADER_TRANSFER_ENCODING: HEADER_VALUE_CHUNKED,
            },
            keep_alive,
        )

    async def stream_to(
        self,
        transport: Any,
        drain: Callable[[], Awaitable[None]] | None = None,
        keep_alive: bool = True,
    ) -> None:
        """Stream SSE events to transport.

        When `drain` is supplied (the raw serving protocol passes its write-side
        flow-control awaitable) it is awaited after each event, so a fast event
        producer feeding a slow client is throttled at the transport buffer
        instead of growing it without bound. It is a no-op until the buffer
        crosses the high-water mark.
        """
        transport.write(self.encode(keep_alive=keep_alive))
        # `_encode_stream` normalised `_stream` to bytes for the chunk writer.
        await _write_chunked(transport, self._stream, drain)

    def _encode_stream(
        self,
        content: AsyncIterator[_SSEItem],
    ) -> AsyncIterator[bytes]:
        """Encode each yielded item to `bytes`.

        Accepts `ServerSentEvent` objects, plain `str`, or already-encoded
        `bytes`; any other value is coerced into a single-`data:` event, a
        `Mapping` becoming a JSON body and a scalar its text.

        When `ping` is set, the wait for the next event is bounded by `ping`
        seconds and a timeout emits a keep-alive frame, so both transports
        inherit heartbeats without any per-transport code.
        """
        if self.ping is None:
            return self._encode_plain(content)
        return self._encode_with_ping(content, self.ping)

    @staticmethod
    def _encode_event(item: Any) -> bytes:
        # Fast paths: the event object and the two already-wire-ready forms.
        if isinstance(item, ServerSentEvent):
            return item.encode()
        if isinstance(item, str):
            return item.encode("utf-8")
        if isinstance(item, (bytes, bytearray)):
            return bytes(item)
        # Coerced rather than left to crash the chunk writer, and through the
        # shared encoder so one stream cannot carry two JSON dialects.
        data = dumps_current(item).decode("utf-8") if isinstance(item, Mapping) else str(item)
        return ServerSentEvent(data=data).encode()

    @classmethod
    async def _encode_plain(
        cls,
        content: AsyncIterator[_SSEItem],
    ) -> AsyncIterator[bytes]:
        async for item in content:
            yield cls._encode_event(item)

    async def _encode_with_ping(
        self,
        content: AsyncIterator[_SSEItem],
        ping: float,
    ) -> AsyncIterator[bytes]:
        # One task wraps each `__anext__` and is awaited again on the next loop,
        # so a ping-window timeout does not cancel into the source generator.
        it = content.__aiter__()
        pending: Any = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(it.__anext__())
                done, _ = await asyncio.wait((pending,), timeout=ping)
                if not done:
                    # No event within the window - keep the connection warm.
                    yield self._ping_frame
                    continue
                task = pending
                pending = None
                try:
                    item = task.result()
                except StopAsyncIteration:
                    return
                yield self._encode_event(item)
        finally:
            if pending is not None:
                pending.cancel()
