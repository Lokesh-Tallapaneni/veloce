"""Streaming responses — a body produced incrementally."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from veloce._constants import (
    HEADER_CONTENT_TYPE,
    HEADER_TRANSFER_ENCODING,
    HEADER_VALUE_CHUNKED,
)
from veloce._internal import (
    MIME_OCTET,
    _write_chunked,
)

# ── Case-insensitive response-header access ──────────────────────────
# `Response.headers` is case-SENSITIVE but HTTP field names are not (RFC 9110
# Sec. 5.1), so these fold the lookup without allocating a dict per call.
from veloce.http.response.response import Response
from veloce.status import (
    HTTP_200_OK,
)


class StreamingResponse(Response):
    """Streaming response for large payloads.

    `content` may be an async iterator/iterable **or** a plain sync
    iterable (e.g. a generator). A sync iterable is wrapped so the
    response always exposes an async stream; both forms are accepted.

    Usage::

        from veloce import StreamingResponse

        async def handler(request):
            def chunks():
                yield b"part-1"
                yield b"part-2"

            return StreamingResponse(chunks(), content_type="text/plain")
    """

    __slots__ = ()

    def __init__(
        self,
        content: Any,
        status_code: int = HTTP_200_OK,
        content_type: str = MIME_OCTET,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            body=b"",
            content_type=content_type,
            headers=headers,
        )
        # Both branches go through an adapter that encodes `str`.
        self._stream: AsyncIterator[bytes] | None = (
            self._aiter_async(content)
            if hasattr(content, "__aiter__")
            else self._aiter_sync(content)
        )

    @staticmethod
    async def _aiter_sync(iterable: Any) -> AsyncIterator[bytes]:
        """Adapt a synchronous iterable into an async iterator.

        `str` chunks are encoded to UTF-8 so downstream byte-only paths
        (chunked transfer encoding) work uniformly.
        """
        for chunk in iterable:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk

    @staticmethod
    async def _aiter_async(iterable: Any) -> AsyncIterator[bytes]:
        """Adapt an async iterable, encoding `str` chunks the way `_aiter_sync` does."""
        async for chunk in iterable:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk

    def encode(self, *, keep_alive: bool = True) -> bytes:
        """For streaming, encode headers with chunked transfer."""
        return self._encode_streaming_head(
            {
                HEADER_CONTENT_TYPE: self.content_type,
                HEADER_TRANSFER_ENCODING: HEADER_VALUE_CHUNKED,
            },
            keep_alive,
        )

    async def stream_to(
        self,
        transport: Any,
        drain: Callable[[], Awaitable[None]] | None = None,
        *,
        keep_alive: bool = True,
    ) -> None:
        """Stream chunks to transport.

        When `drain` is supplied (the raw serving protocol passes its write-side
        flow-control awaitable) it is awaited after each chunk, so a producer
        outrunning a slow client is throttled instead of growing the transport
        write buffer without bound. `drain` is a no-op until the buffer crosses
        the high-water mark, so the fast path pays one already-set check.

        Args:
            transport: The raw transport to write to.
            drain: Awaited after each chunk to apply flow control; `None` skips it.
            keep_alive: Whether to advertise a persistent connection.

        """
        transport.write(self.encode(keep_alive=keep_alive))
        # A 304 drops the stream with the body (RFC 9110 Sec. 15.4.5).
        if self._stream is not None:
            await _write_chunked(transport, self._stream, drain)
