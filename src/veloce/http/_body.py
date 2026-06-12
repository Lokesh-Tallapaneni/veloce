"""Request-body source — async incremental body for headers-complete dispatch.

The raw HTTP/1.1 protocol dispatches a request once its headers are parsed,
before the body has fully arrived. Body bytes are then fed in as the socket
delivers them. `RequestBodySource` is the buffer between the parser callbacks
(`feed` / `feed_eof`) and the handler's async reads (`stream` / `read`).

A handler that never reads the body must not strand unparsed bytes that would
corrupt the next pipelined request, so the source also supports a `drain()`
that discards anything unread once EOF is known.

Backpressure is applied across socket reads: when the number of unconsumed
chunks reaches a high-water mark the source asks the protocol to pause socket
reading, and once a consumer drains back below the low-water mark it asks the
protocol to resume. This bounds how many *future* reads pile up in front of a
slow handler. It does not bound a single read: `pause_reading` only stops the
event loop scheduling further `data_received` calls, so all the chunked frames
already present in one TCP segment are delivered (and buffered) in one parser
pass before the handler runs. The hard memory cap on a single segment is the OS
socket receive buffer; the hard cap on the whole body is `max_content_length`.
Pause/resume are wired to the transport's flow control; on the in-memory
ASGI/TestClient path no callbacks are installed, so backpressure is a no-op
there.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable

from veloce.exceptions import RequestEntityTooLarge

# Bound on unconsumed body chunks. Reaching the high-water mark pauses socket
# reading; draining back to the low-water mark resumes it. The gap (hysteresis)
# avoids thrashing pause/resume on every single chunk around the boundary.
DEFAULT_HIGH_WATER_CHUNKS = 16
DEFAULT_LOW_WATER_CHUNKS = 4


class RequestBodySource:
    """Async producer/consumer queue for one request's body bytes.

    The protocol calls `feed(chunk)` from `on_body` and `feed_eof()` from
    `on_message_complete`. The handler consumes via `__aiter__` (chunk at a
    time) or `read()` (everything to EOF). A single waiter is supported - one
    handler consumes one request's body - which matches the per-connection
    FIFO server loop (one in-flight request at a time).

    `max_content_length` caps the running total: feeding past it flips an
    overflow latch, and the next read raises `RequestEntityTooLarge` so the
    dispatch layer renders a 413 mid-stream instead of buffering an unbounded
    body. The protocol additionally rejects on the declared Content-Length
    header before any body byte is read.

    Backpressure bounds unconsumed chunks *across socket reads*: when `feed`
    pushes the count to `high_water` the `pause` callback fires (the protocol
    pauses socket reading); when a consumer drains the count back to
    `low_water` the `resume` callback fires. Pausing does not cap a single
    read - every chunked frame in one delivered segment is fed in one pass
    before the handler runs - so the true memory cap is `max_content_length`,
    not the chunk count. Both callbacks default to no-ops, so the in-memory
    ASGI/TestClient path (which pre-fills the body) is unaffected.
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
        # Set whenever a chunk arrives or EOF is signalled, so a consumer
        # blocked in `__anext__` wakes promptly. Cleared once drained.
        self._event = asyncio.Event()
        self._size = 0
        self._max = max_content_length
        self._overflow = False
        self._high_water = high_water
        self._low_water = low_water
        # Flow-control hooks the protocol installs via `set_flow_control`.
        # No-ops by default so the pre-filled ASGI/TestClient path never pauses.
        self._pause: Callable[[], None] | None = None
        self._resume: Callable[[], None] | None = None
        self._paused = False
        # Set while `drain()` is discarding an unread body. The connection is
        # being torn down, so it must stay unpaused for the whole drain: `feed`
        # never re-pauses while this is set, otherwise a re-pause mid-drain
        # would stop the remaining body (and EOF) from ever arriving.
        self._draining = False

    def set_flow_control(self, pause: Callable[[], None], resume: Callable[[], None]) -> None:
        """Wire pause/resume callbacks (the transport's flow control).

        Called once by the protocol when it attaches the source to a live
        connection. The in-memory path never calls this, leaving backpressure
        a no-op.
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

        The single source of truth for the streamed body size cap: `feed`
        latches this the moment the total crosses the limit, so the protocol
        does not keep an independent byte counter that could drift from it.
        """
        return self._overflow

    def feed(self, chunk: bytes) -> None:
        """Append a body chunk; flip the overflow latch if the cap is passed.

        When the buffered chunk count reaches the high-water mark, pause socket
        reading so a fast producer cannot outrun a slow consumer without bound.
        """
        if not chunk:
            return
        if self._draining:
            # Teardown is discarding the body. Drop the bytes immediately and
            # wake the parked drain; never pause, so the remaining body and its
            # terminating EOF keep flowing to completion.
            self._size = 0
            self._event.set()
            return
        self._size += len(chunk)
        if self._max is not None and self._size > self._max:
            # Drop the over-limit bytes - we will refuse with 413 on read and
            # the connection is going to be torn down, so retaining them is
            # pure memory pressure.
            self._overflow = True
            self._event.set()
            return
        self._chunks.append(chunk)
        self._event.set()
        if not self._paused and len(self._chunks) >= self._high_water and self._pause is not None:
            self._paused = True
            self._pause()

    def feed_eof(self) -> None:
        """Signal that no more body bytes will arrive."""
        self._eof = True
        self._event.set()

    def _maybe_resume(self) -> None:
        """Resume socket reading once the buffer drains to the low-water mark."""
        if self._paused and len(self._chunks) <= self._low_water:
            self._paused = False
            if self._resume is not None:
                self._resume()

    def _check_overflow(self) -> None:
        if self._overflow:
            raise RequestEntityTooLarge(f"Request body exceeds the {self._max}-byte limit")

    def __aiter__(self) -> RequestBodySource:
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

        Called on request teardown so a body-ignoring handler cannot leave
        unconsumed bytes that the protocol would otherwise misread as the
        start of the next pipelined request. Overflow is swallowed here - the
        connection that overflowed is already being closed.

        If the source paused reading on the producer side, this resumes it and
        latches `_draining` so `feed` never re-pauses for the rest of the
        drain: a paused connection delivers no further bytes and never reaches
        EOF, so any re-pause mid-drain (as the resumed socket delivers more
        body past the high-water mark) would deadlock - the body and its
        terminating EOF would never arrive.
        """
        self._draining = True
        if self._paused:
            self._paused = False
            if self._resume is not None:
                self._resume()
        while not self._eof:
            self._chunks.clear()
            self._size = 0  # reset so the discarded bytes don't trip the cap
            self._event.clear()
            await self._event.wait()
        self._chunks.clear()
