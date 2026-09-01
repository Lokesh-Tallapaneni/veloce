"""A streaming body yields bytes to the transport, whichever iterator produced it.

`_aiter_sync` promised that `str` chunks are encoded "so downstream byte-only
paths work uniformly", but only the synchronous branch went through it. An
async generator yielding `str` was assigned straight to a slot declared
`AsyncIterator[bytes]` and crashed the native transport with a `TypeError`.
"""

from __future__ import annotations

import pytest

from veloce import StreamingResponse


class FakeTransport:
    """A write-only transport double, so these tests need no socket."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)


async def _async_str_chunks():
    yield "hello"
    yield "world"


def _sync_str_chunks():
    yield "hello"
    yield "world"


async def _async_bytes_chunks():
    yield b"hello"


async def test_an_async_generator_of_str_is_encoded_for_the_transport():
    """POSITIVE: the async branch now honours the encoding contract."""
    response = StreamingResponse(content=_async_str_chunks(), content_type="text/plain")
    transport = FakeTransport()

    await response.stream_to(transport)

    assert b"5\r\nhello\r\n" in transport.writes
    assert b"5\r\nworld\r\n" in transport.writes
    assert all(isinstance(w, bytes) for w in transport.writes)


async def test_a_sync_generator_of_str_still_behaves_the_same():
    """NEGATIVE: the branch that already worked must not change.

    Both branches now share one adapter, so this pins that the working half
    was not broken while fixing the other.
    """
    response = StreamingResponse(content=_sync_str_chunks(), content_type="text/plain")
    transport = FakeTransport()

    await response.stream_to(transport)

    assert b"5\r\nhello\r\n" in transport.writes
    assert b"5\r\nworld\r\n" in transport.writes


async def test_an_async_generator_of_bytes_is_passed_through_unchanged():
    """NEGATIVE: bytes must not be double-encoded or otherwise touched."""
    response = StreamingResponse(content=_async_bytes_chunks(), content_type="text/plain")
    transport = FakeTransport()

    await response.stream_to(transport)

    assert b"5\r\nhello\r\n" in transport.writes


async def test_a_chunk_that_is_neither_str_nor_bytes_still_fails_loudly():
    """NEGATIVE: the adapter encodes `str`; it must not silently swallow junk.

    A chunk of the wrong type is a bug in the producing handler, and the
    transport raising is what surfaces it. Coercing it here would hide the
    author's mistake the way the original `Any` hid this one.
    """

    async def bad_chunks():
        yield 42

    response = StreamingResponse(content=bad_chunks(), content_type="text/plain")

    with pytest.raises(TypeError):
        await response.stream_to(FakeTransport())
