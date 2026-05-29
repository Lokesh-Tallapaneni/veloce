"""Async request-body source for incremental (headers-complete) dispatch.

The raw HTTP/1.1 protocol dispatches a request once its headers are parsed,
before the body has fully arrived. Body bytes are then fed in as the socket
delivers them. `RequestBodySource` is the buffer between the parser callbacks
(`feed` / `feed_eof`) and the handler's async reads (`stream` / `read`).

A handler that never reads the body must not strand unparsed bytes that would
corrupt the next pipelined request, so the source also supports a `drain()`
that discards anything unread once EOF is known.
"""

from __future__ import annotations

import asyncio
from collections import deque

from veloce.exceptions import RequestEntityTooLarge


class RequestBodySource:
    """Async producer/consumer queue for one request's body bytes.

    The protocol calls `feed(chunk)` from `on_body` and `feed_eof()` from
    `on_message_complete`. The handler consumes via `__aiter__` (chunk at a
    time) or `read()` (everything to EOF). A single waiter is supported — one
    handler consumes one request's body — which matches the per-connection
    FIFO server loop (one in-flight request at a time).

    `max_content_length` caps the running total: feeding past it flips an
    overflow latch, and the next read raises `RequestEntityTooLarge` so the
    dispatch layer renders a 413 mid-stream instead of buffering an unbounded
    body. The protocol additionally rejects on the declared Content-Length
    header before any body byte is read.
    """

    __slots__ = (
        "_chunks",
        "_eof",
        "_event",
        "_size",
        "_max",
        "_overflow",
    )

    def __init__(self, max_content_length: int | None = None) -> None:
        self._chunks: deque[bytes] = deque()
        self._eof = False
        # Set whenever a chunk arrives or EOF is signalled, so a consumer
        # blocked in `__anext__` wakes promptly. Cleared once drained.
        self._event = asyncio.Event()
        self._size = 0
        self._max = max_content_length
        self._overflow = False

    @property
    def total_bytes(self) -> int:
        """Running total of body bytes fed so far."""
        return self._size

    @property
    def at_eof(self) -> bool:
        """Whether EOF has been signalled and all chunks consumed."""
        return self._eof and not self._chunks

    def feed(self, chunk: bytes) -> None:
        """Append a body chunk; flip the overflow latch if the cap is passed."""
        if not chunk:
            return
        self._size += len(chunk)
        if self._max is not None and self._size > self._max:
            # Drop the over-limit bytes — we will refuse with 413 on read and
            # the connection is going to be torn down, so retaining them is
            # pure memory pressure.
            self._overflow = True
            self._event.set()
            return
        self._chunks.append(chunk)
        self._event.set()

    def feed_eof(self) -> None:
        """Signal that no more body bytes will arrive."""
        self._eof = True
        self._event.set()

    def _check_overflow(self) -> None:
        if self._overflow:
            raise RequestEntityTooLarge(f"Request body exceeds the {self._max}-byte limit")

    def __aiter__(self) -> RequestBodySource:
        return self

    async def __anext__(self) -> bytes:
        while True:
            if self._chunks:
                return self._chunks.popleft()
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
        start of the next pipelined request. Overflow is swallowed here — the
        connection that overflowed is already being closed.
        """
        while not self._eof:
            self._chunks.clear()
            self._size = 0  # reset so the discarded bytes don't trip the cap
            self._event.clear()
            await self._event.wait()
        self._chunks.clear()
