"""Server-Sent Events (SSE) — streaming event responses."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import orjson

from veloce._constants import (
    HEADER_CACHE_CONTROL,
    HEADER_CONNECTION,
    HEADER_CONTENT_TYPE,
    HEADER_TRANSFER_ENCODING,
    HEADER_VALUE_CHUNKED,
    HEADER_VALUE_KEEP_ALIVE,
    HEADER_VALUE_NO_CACHE,
    HEADER_X_ACCEL_BUFFERING,
    MIME_TEXT_EVENT_STREAM,
)
from veloce._internal import _encode_response_head
from veloce.encoders import orjson_default
from veloce.http.response import Response
from veloce.json_provider import dumps_current
from veloce.status import HTTP_200_OK

# SSE keep-alive frame: a comment line (colon-prefixed) the spec requires
# clients to ignore. Sent when no event arrives within the `ping` window
# so idle connections survive proxy/load-balancer read timeouts.
_PING_FRAME = b": ping\r\n\r\n"

# What an SSE source iterator may yield. A `ServerSentEvent` is encoded
# directly; `str`/`bytes` are taken as a ready event body; any other value
# (a `Mapping`, an `int`/`float`, etc.) is coerced into a single-`data:`
# event by `_encode_event` rather than crashing the chunk writer.
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
        # WHATWG SSE: `event` and `id` are single-line fields - a CR/LF would
        # silently split or truncate them on the wire. A NUL in `id` makes the
        # client ignore the id entirely, breaking Last-Event-ID reconnection.
        # Reject these at construction rather than silently stripping, so the
        # bug surfaces at the source. Non-str values are coerced first (an int
        # id stays valid, as before), then validated. `data` stays permissive
        # (it is line-split into multiple `data:` fields by `encode`). `data`
        # may be None to emit a comment-only event (no `data:` lines).
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
        # WHATWG SSE: a line beginning with `:` is a comment the client
        # ignores. Multi-line comments are line-split like `data`, so the
        # field stays permissive (no newline rejection).
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

        Serialization runs once here, off the per-event stream loop, and the
        result is stored in the plain `data` field - so `encode()` stays the
        same branch-free path it is for a raw `data=` string. Use the regular
        constructor when the payload is already a formatted string.
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
            # Comments precede the event fields so clients (and proxies) see
            # them first. Line-split like `data` so an embedded newline emits
            # one `: ` line per segment instead of a mangled single line.
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
            # `not finite` rejects NaN (fails every comparison, so `<= 0` lets
            # it slip through) and Infinity (passes `> 0` but is meaningless as
            # an `asyncio.wait` timeout - the heartbeat would never fire).
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
            # Precompute the keep-alive frame once. Build it directly (not via
            # ServerSentEvent.encode) to preserve the CRLF framing the default
            # `_PING_FRAME` constant uses.
            self._ping_frame = f": {ping_comment}\r\n\r\n".encode()
        else:
            self._ping_frame = _PING_FRAME
        hdrs = dict(headers) if headers else {}
        hdrs.update(
            {
                HEADER_CACHE_CONTROL: HEADER_VALUE_NO_CACHE,
                HEADER_CONNECTION: HEADER_VALUE_KEEP_ALIVE,
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
        # Normalise every yielded item to bytes up front, so the ASGI
        # transport and the raw-socket transport consume an identical
        # `bytes` stream - see `_encode_stream`.
        self._stream = self._encode_stream(content)

    def encode(self) -> bytes:
        """Encode the event-stream head.

        `EventSourceResponse` extends `Response`, not `StreamingResponse`, so it
        inherited the buffered `encode()` - and the native transport calls
        `encode()` to answer a HEAD. A HEAD on an SSE route therefore advertised
        `Content-Length: 0` for a resource whose GET streams indefinitely, which
        a caching intermediary may act on. RFC 9110 Sec. 9.3.2 requires the
        header section a GET would have produced.

        `stream_to` writes this same head, so the two cannot come to describe
        the response differently.
        """
        default_headers = {
            HEADER_CONTENT_TYPE: self.content_type,
            HEADER_CACHE_CONTROL: HEADER_VALUE_NO_CACHE,
            HEADER_CONNECTION: HEADER_VALUE_KEEP_ALIVE,
            HEADER_TRANSFER_ENCODING: HEADER_VALUE_CHUNKED,
        }
        parts = _encode_response_head(self.status_code, default_headers, self.headers)
        parts.append("\r\n")
        return "".join(parts).encode("latin-1")

    async def stream_to(
        self,
        transport: Any,
        drain: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Stream SSE events to transport.

        When `drain` is supplied (the raw serving protocol passes its write-side
        flow-control awaitable) it is awaited after each event, so a fast event
        producer feeding a slow client is throttled at the transport buffer
        instead of growing it without bound. It is a no-op until the buffer
        crosses the high-water mark.
        """
        transport.write(self.encode())

        async for chunk in self._stream:
            # `_stream` is normalised to bytes by `_encode_stream`.
            # `writelines` keeps the size-line, payload, and trailer as
            # separate buffers instead of concatenating them into a fresh
            # bytes object per chunk.
            size = format(len(chunk), "x").encode("ascii")
            transport.writelines((size, b"\r\n", chunk, b"\r\n"))
            if drain is not None:
                await drain()

        transport.write(b"0\r\n\r\n")

    def _encode_stream(
        self,
        content: AsyncIterator[_SSEItem],
    ) -> AsyncIterator[bytes]:
        """Encode each yielded item to `bytes`.

        Accepts `ServerSentEvent` objects (encoded via `.encode()`), plain
        `str` (UTF-8 encoded), or already-encoded `bytes`. Any other yielded
        value is coerced into a single-`data:` event (a `Mapping` becomes a
        JSON body, a scalar its text). This keeps both transports consistent:
        the ASGI streaming branch and `stream_to` each receive `bytes`
        regardless of what the handler yields.

        When `ping` is set, the wait for the next event is bounded by
        `ping` seconds; on timeout a keep-alive comment frame is emitted
        and the wait restarts, so both transports inherit heartbeats
        without any per-transport code.
        """
        if self.ping is None:
            return self._encode_plain(content)
        return self._encode_with_ping(content, self.ping)

    # `_encode_with_ping` is an instance method so it can read the precomputed
    # `self._ping_frame` (default or custom token); `_encode_plain` stays a
    # classmethod since it needs no per-response state.

    @staticmethod
    def _encode_event(item: Any) -> bytes:
        # Fast paths first: the framework's own event object and the two
        # already-wire-ready forms a handler commonly yields.
        if isinstance(item, ServerSentEvent):
            return item.encode()
        if isinstance(item, str):
            return item.encode("utf-8")
        if isinstance(item, (bytes, bytearray)):
            return bytes(item)
        # Coerce a bare yielded value into a single-`data:` event rather than
        # letting a non-bytes object fall through and crash the chunk writer.
        # A Mapping serialises to a JSON `data:` field; a scalar uses its text.
        # `bool` is a subclass of `int`, so its `True`/`False` text is fine.
        if isinstance(item, Mapping):
            data = orjson.dumps(item, default=orjson_default).decode("utf-8")
        else:
            data = str(item)
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
        # A single task wraps each `__anext__` so a ping-window timeout
        # does NOT cancel the in-flight pull - cancelling would throw
        # into the generator and kill it. We await the SAME task again
        # on the next loop; it resolves once the source finally yields.
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
