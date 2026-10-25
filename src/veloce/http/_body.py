"""Request-body source — async incremental body for headers-complete dispatch.

The raw HTTP/1.1 protocol dispatches a request once its headers are parsed,
before the body has fully arrived. Body bytes are then fed in as the socket
delivers them. `RequestBodySource` is the buffer between the parser callbacks
(`feed` / `feed_eof`) and the handler's async reads (`stream` / `read`).

A handler that never reads the body must not strand unparsed bytes that would
corrupt the next pipelined request, so the source also supports a `drain()`
that discards anything unread once EOF is known.

Backpressure bounds how many *future* socket reads pile up in front of a slow
handler, not a single read: `pause_reading` only stops the loop scheduling
further `data_received` calls, so every chunked frame already in one TCP
segment is buffered in one parser pass. The hard cap on the whole body is
`max_content_length`. On the in-memory ASGI/TestClient path no pause/resume
callbacks are installed, so backpressure is a no-op there.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from typing import Any

from veloce._constants import MSG_REQUEST_BODY_EXCEEDS_MAX
from veloce._internal import _require_methods
from veloce._protocol_constants import (
    ASGI_EVENT_HTTP_DISCONNECT,
    ASGI_EVENT_HTTP_REQUEST,
)
from veloce.exceptions import RequestEntityTooLarge
from veloce.status import HTTP_413_CONTENT_TOO_LARGE

_logger = logging.getLogger(__name__)

# Bound on unconsumed body chunks. The gap is hysteresis: it avoids thrashing
# pause/resume on every single chunk around the boundary.
DEFAULT_HIGH_WATER_CHUNKS = 16
DEFAULT_LOW_WATER_CHUNKS = 4


def body_too_large(limit: int | None) -> RequestEntityTooLarge:
    """Build the refusal a body source raises, carrying the limit it tripped.

    The limit rides on the exception so the rendered body matches the one the
    eager refusal paths emit through `too_large_payload`.
    """
    exc = RequestEntityTooLarge(MSG_REQUEST_BODY_EXCEEDS_MAX)
    exc.limit = limit
    return exc


def too_large_payload(limit: int | None) -> dict[str, Any]:
    """Build the body both transports answer a `MAX_CONTENT_LENGTH` refusal with.

    Built here so the two paths cannot describe the same refusal differently.
    """
    return {
        "detail": MSG_REQUEST_BODY_EXCEEDS_MAX,
        "status_code": HTTP_413_CONTENT_TOO_LARGE,
        "limit": limit,
    }


class BodySource:
    """The consumer interface a request body source presents, on any transport.

    `Request` is transport-agnostic: it holds whichever source built it and
    consumes through this interface alone, though the two implementations arrive
    at it from opposite directions - the native one push-fed by the HTTP/1.1
    parser, the ASGI one pulling from a `receive` channel.

    `poll_disconnected` is the one a consumer calls, and it is a coroutine
    because a channel that has to be *asked* about the drop needs the call to go
    and ask. `disconnected` is the same latch read without asking anything.
    """

    __slots__ = ()

    #: What a source must supply, checked at definition: one missing a member
    #: works until a handler reaches for it, and then fails mid-body.
    _required = ("__aiter__", "__anext__", "read", "poll_disconnected")

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        _require_methods(cls, BodySource, BodySource._required)

    def __aiter__(self) -> BodySource:
        """Iterate the body chunk by chunk, as the transport delivers it."""
        raise NotImplementedError

    async def __anext__(self) -> bytes:
        """Return the next chunk, or raise `StopAsyncIteration` at EOF."""
        raise NotImplementedError

    async def read(self) -> bytes:
        """Pull every remaining chunk to EOF and return the joined bytes."""
        raise NotImplementedError

    @property
    def disconnected(self) -> bool:
        """`True` when the stream ended because the peer went away."""
        raise NotImplementedError

    async def poll_disconnected(self) -> bool:
        """`True` when the peer has gone, asking the transport if it must."""
        raise NotImplementedError


class RequestBodySource(BodySource):
    """Async producer/consumer queue for one request's body bytes.

    The protocol calls `feed(chunk)` from `on_body` and `feed_eof()` from
    `on_message_complete`; the handler consumes via `__aiter__` or `read()`. A
    single waiter is supported, matching the per-connection FIFO server loop.

    `max_content_length` caps the running total: feeding past it flips an
    overflow latch and the next read raises `RequestEntityTooLarge`, so a 413 is
    rendered mid-stream rather than an unbounded body buffered.

    `feed` fires `pause` at `high_water` and a draining consumer fires `resume`
    at `low_water`. Both default to no-ops, so the in-memory ASGI/TestClient
    path is unaffected.
    """

    __slots__ = (
        "_chunks",
        "_eof",
        "_event",
        "_size",
        "_max",
        "_overflow",
        "_high_water",
        "_low_water",
        "_pause",
        "_resume",
        "_paused",
        "_draining",
        "_disconnected",
    )

    def __init__(
        self,
        max_content_length: int | None = None,
        *,
        high_water: int = DEFAULT_HIGH_WATER_CHUNKS,
        low_water: int = DEFAULT_LOW_WATER_CHUNKS,
    ) -> None:
        self._chunks: deque[bytes] = deque()
        self._eof = False
        # Set on a chunk or EOF, so a consumer blocked in `__anext__` wakes.
        self._event = asyncio.Event()
        self._size = 0
        self._max = max_content_length
        self._overflow = False
        self._high_water = high_water
        self._low_water = low_water
        # No-ops by default, so the pre-filled ASGI path never pauses.
        self._pause: Callable[[], None] | None = None
        self._resume: Callable[[], None] | None = None
        self._paused = False
        # Set while `drain()` is discarding an unread body, which must stay
        # unpaused throughout or the remaining body and EOF never arrive.
        self._draining = False
        self._disconnected = False

    def set_flow_control(self, pause: Callable[[], None], resume: Callable[[], None]) -> None:
        """Wire pause/resume callbacks (the transport's flow control).

        Called once by the protocol when it attaches the source to a live
        connection; the in-memory path never calls it.
        """
        self._pause = pause
        self._resume = resume

    @property
    def total_bytes(self) -> int:
        """Running total of body bytes fed so far."""
        return self._size

    @property
    def at_eof(self) -> bool:
        """Whether EOF has been signalled and all chunks consumed."""
        return self._eof and not self._chunks

    @property
    def overflowed(self) -> bool:
        """Whether the running byte total has passed max_content_length.

        The single source of truth for the streamed body size cap, so the
        protocol keeps no independent byte counter that could drift from it.
        """
        return self._overflow

    def feed(self, chunk: bytes) -> None:
        """Append a body chunk; flip the overflow latch if the cap is passed.

        At the high-water mark socket reading pauses, so a fast producer cannot
        outrun a slow consumer without bound.
        """
        if not chunk:
            return
        if self._draining:
            # Dropped and the parked drain woken; never paused, so the remaining
            # body and its terminating EOF keep flowing.
            self._size = 0
            self._event.set()
            return
        self._size += len(chunk)
        if self._max is not None and self._size > self._max:
            # Dropped: the read refuses with 413 and the connection is torn
            # down, so retaining them is pure memory pressure.
            self._overflow = True
            self._event.set()
            return
        self._chunks.append(chunk)
        self._event.set()
        if not self._paused and len(self._chunks) >= self._high_water and self._pause is not None:
            self._paused = True
            self._pause()

    def feed_eof(self, *, disconnected: bool = False) -> None:
        """Signal that no more body bytes will arrive.

        `disconnected=True` says the peer went away rather than the body
        completing. Both end the stream, but without the distinction
        `request.is_disconnected()` answers `False` for a client that is gone.
        """
        self._eof = True
        if disconnected:
            self._disconnected = True
        self._event.set()

    def mark_disconnected(self) -> None:
        """Record that the peer went away, leaving the body's EOF state alone.

        A handler streaming its *response* has a body that already completed, so
        there is no stream left to end - only the fact to record.
        """
        self._disconnected = True

    @property
    def disconnected(self) -> bool:
        """`True` when the stream ended because the peer went away."""
        return self._disconnected

    async def poll_disconnected(self) -> bool:
        """`True` when the peer has gone - the protocol pushes it, so just read it."""
        return self._disconnected

    def _maybe_resume(self) -> None:
        """Resume socket reading once the buffer drains to the low-water mark."""
        if self._paused and len(self._chunks) <= self._low_water:
            self._paused = False
            if self._resume is not None:
                self._resume()

    def _check_overflow(self) -> None:
        if self._overflow:
            raise body_too_large(self._max)

    def __aiter__(self) -> RequestBodySource:
        """Iterate the fed chunks, waiting when the queue is empty."""
        return self

    async def __anext__(self) -> bytes:
        while True:
            if self._chunks:
                chunk = self._chunks.popleft()
                self._maybe_resume()
                return chunk
            self._check_overflow()
            if self._eof:
                raise StopAsyncIteration
            self._event.clear()
            await self._event.wait()

    async def read(self) -> bytes:
        """Pull every remaining chunk to EOF and return the joined bytes."""
        parts: list[bytes] = []
        async for chunk in self:
            parts.append(chunk)
        return b"".join(parts)

    async def drain(self) -> None:
        """Discard any unread body, waiting for EOF if the body is still arriving.

        Called on request teardown, so a body-ignoring handler cannot leave
        bytes the protocol would misread as the next pipelined request.
        `_draining` latches so `feed` never re-pauses: a paused connection
        delivers nothing further and never reaches EOF, which would deadlock.
        """
        self._draining = True
        if self._paused:
            self._paused = False
            if self._resume is not None:
                self._resume()
        while not self._eof:
            self._chunks.clear()
            # Reset so the discarded bytes do not count toward the cap.
            self._size = 0
            self._event.clear()
            await self._event.wait()
        self._chunks.clear()


def _discard_outcome(task: asyncio.Future[Any]) -> None:
    """Read an abandoned task's outcome so asyncio does not log it."""
    if not task.cancelled():
        task.exception()


class ASGIBodySource(BodySource):
    """Pull-based body source over an ASGI `receive` channel.

    Presents the same consumer interface as the push-fed `RequestBodySource`, so
    a `Request` built on either transport streams identically and the dispatch
    layer is unaware of which is underneath.

    `max_content_length` caps the running total: a chunk past the limit raises
    `RequestEntityTooLarge`, so an over-large streamed body is refused mid-read.
    A `http.disconnect` ends the stream, truncated at the bytes received.

    `body_complete=True` builds one over a channel whose body is already in
    hand, so the source exists only to poll for the drop.
    """

    __slots__ = ("_receive", "_max", "_size", "_done", "_disconnected")

    def __init__(
        self,
        receive: Callable[[], Any],
        max_content_length: int | None = None,
        *,
        body_complete: bool = False,
    ) -> None:
        self._receive = receive
        self._max = max_content_length
        self._size = 0
        self._done = body_complete
        # `_done` covers both "body complete" and "peer vanished"; this keeps the
        # second apart so `Request.is_disconnected()` can answer truthfully.
        self._disconnected = False

    @property
    def disconnected(self) -> bool:
        """`True` when the stream ended because the peer went away."""
        return self._disconnected

    async def poll_disconnected(self) -> bool:
        """`True` when the peer has gone, asked of the channel without blocking.

        An ASGI server reports the drop as an `http.disconnect` and nowhere
        else, so a handler streaming a response can only learn it by asking.
        `receive()` runs as a task for exactly one loop turn: a message already
        waiting is seen, while a channel with nothing to say is cancelled
        unconsumed. The answer latches, because a server hands it out once.

        `False` while the body is still arriving: the handler consuming it owns
        the channel, and that consumer sees the drop through `disconnected`.
        """
        if self._disconnected:
            return True
        if not self._done:
            return False
        task = asyncio.ensure_future(self._receive())
        await asyncio.sleep(0)
        if not task.done():
            task.cancel()
            # The task is abandoned, and an unretrieved exception would log
            # once per poll of a streaming handler.
            task.add_done_callback(_discard_outcome)
            return False
        if task.cancelled() or task.exception() is not None:
            # No answer is not an answer: reporting a live client as gone would
            # abort the stream it is still reading.
            return False
        message = task.result()
        if message.get("type") == ASGI_EVENT_HTTP_DISCONNECT:
            self._disconnected = True
            return True
        # Nothing can be handed this, since `_done` is the precondition for
        # polling. Logged, or a body goes missing with nothing to point at.
        _logger.warning(
            "Disconnect poll consumed a %s message that no reader can take; "
            "the receive channel is shared with another consumer",
            message.get("type"),
        )
        return False

    def __aiter__(self) -> ASGIBodySource:
        return self

    async def __anext__(self) -> bytes:
        while True:
            if self._done:
                raise StopAsyncIteration
            message = await self._receive()
            mtype = message.get("type")
            if mtype == ASGI_EVENT_HTTP_DISCONNECT:
                self._done = True
                self._disconnected = True
                raise StopAsyncIteration
            if mtype != ASGI_EVENT_HTTP_REQUEST:
                # Ignore any non-body control message and keep pulling.
                continue
            chunk = message.get("body", b"") or b""
            if not message.get("more_body", False):
                self._done = True
            if chunk:
                self._size += len(chunk)
                if self._max is not None and self._size > self._max:
                    raise body_too_large(self._max)
                return chunk
            if self._done:
                raise StopAsyncIteration

    async def read(self) -> bytes:
        """Pull the body to EOF and return the joined bytes.

        Fast-paths a whole (or empty) body arriving in one `http.request`
        message: a single `receive()`, no iterator step and no list/join, so a
        buffered route pays nothing extra. Only a multi-chunk body accumulates.
        """
        if self._done:
            return b""
        parts: list[bytes] | None = None
        while not self._done:
            message = await self._receive()
            mtype = message.get("type")
            if mtype == ASGI_EVENT_HTTP_DISCONNECT:
                self._done = True
                self._disconnected = True
                break
            if mtype != ASGI_EVENT_HTTP_REQUEST:
                continue
            last = not message.get("more_body", False)
            if last:
                self._done = True
            chunk = message.get("body", b"") or b""
            if chunk:
                self._size += len(chunk)
                if self._max is not None and self._size > self._max:
                    raise body_too_large(self._max)
                if parts is None:
                    if last:
                        # Whole body arrived in one message: skip the list/join.
                        return chunk
                    parts = [chunk]
                else:
                    parts.append(chunk)
        return b"".join(parts) if parts else b""
