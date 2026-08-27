"""Write-side backpressure: the drain protocol and the write-buffer limits.

Split out of `test_server_protocol.py`, which ran to 2,112 lines across ten
protocol concerns with two section separators - both of them past line
1,667, marking exactly this seam.
"""

from __future__ import annotations

import asyncio

from tests._protocol import _FakeTransport
from veloce import Veloce
from veloce.serving.protocol import (
    HttpProtocol,
)


class _LimitTransport(_FakeTransport):
    """Fake transport that records the write-buffer limit handed to it."""

    def __init__(self) -> None:
        super().__init__()
        self.high_limit: int | None = None

    def set_write_buffer_limits(self, high: int | None = None, low: int | None = None) -> None:
        self.high_limit = high


def test_connection_made_arms_write_buffer_limit():
    """connection_made hands a high-water mark to the transport so asyncio
    fires pause_writing / resume_writing for the streaming path to await on."""
    from veloce.serving.protocol import WRITE_BUFFER_HIGH_WATER

    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _LimitTransport()
        proto.connection_made(transport)

        assert transport.high_limit == WRITE_BUFFER_HIGH_WATER
        # The write gate starts open so the common path never blocks.
        assert proto._can_write.is_set() is True
    finally:
        loop.close()


class _UvloopLikeTransport:
    """A full-duplex transport that is NOT an `asyncio.Transport` subclass.

    Mirrors uvloop's `TCPTransport`, which implements the transport interface
    without inheriting `asyncio.Transport`. The capability check must accept it.
    """

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


class _WriteOnlyTransport:
    """Half-duplex: write side only, no `pause_reading` — must be rejected."""

    def write(self, data: bytes) -> None:
        pass


def test_connection_made_accepts_uvloop_like_transport():
    """A capability-compatible transport that is not an `asyncio.Transport`
    subclass (e.g. uvloop's) is accepted, so `Veloce.run()` works under uvloop."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _UvloopLikeTransport()
        assert not isinstance(transport, asyncio.Transport)
        proto.connection_made(transport)
        assert proto.transport is transport
        proto.connection_lost(None)
    finally:
        loop.close()


def test_write_buffer_limit_honours_config_override():
    """A WRITE_BUFFER_HIGH_WATER config override is passed through verbatim."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["WRITE_BUFFER_HIGH_WATER"] = 4096
        proto = HttpProtocol(app, loop)
        transport = _LimitTransport()
        proto.connection_made(transport)

        assert transport.high_limit == 4096
    finally:
        loop.close()


def test_drain_returns_immediately_when_writable():
    """The fast path: drain() does not block while the gate is set."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        proto.connection_made(_LimitTransport())

        # Should complete without ever scheduling a wait.
        loop.run_until_complete(asyncio.wait_for(proto.drain(), timeout=0.1))
    finally:
        loop.close()


def test_pause_writing_blocks_drain_until_resume():
    """pause_writing parks drain(); resume_writing releases it."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        proto.connection_made(_LimitTransport())

        proto.pause_writing()
        assert proto._can_write.is_set() is False

        async def _scenario() -> bool:
            waiter = asyncio.ensure_future(proto.drain())
            # Give the waiter a tick to park on the cleared gate.
            await asyncio.sleep(0)
            assert not waiter.done()
            proto.resume_writing()
            await asyncio.wait_for(waiter, timeout=0.1)
            return waiter.done()

        assert loop.run_until_complete(_scenario()) is True
    finally:
        loop.close()


def test_connection_lost_releases_parked_writer():
    """A stream parked in drain() is released when the client disconnects so
    it fails fast on its next write instead of hanging forever."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _LimitTransport()
        proto.connection_made(transport)

        proto.pause_writing()

        async def _scenario() -> None:
            waiter = asyncio.ensure_future(proto.drain())
            await asyncio.sleep(0)
            assert not waiter.done()
            proto.connection_lost(None)
            await asyncio.wait_for(waiter, timeout=0.1)

        loop.run_until_complete(_scenario())
    finally:
        loop.close()


def test_streaming_response_awaits_drain_per_chunk():
    """StreamingResponse.stream_to awaits the supplied drain after every chunk
    so a fast producer is throttled at the transport buffer."""
    from veloce.http.response import StreamingResponse

    loop = asyncio.new_event_loop()
    try:

        async def _gen():
            yield b"a"
            yield b"b"
            yield b"c"

        drained = 0

        async def _drain() -> None:
            nonlocal drained
            drained += 1

        resp = StreamingResponse(_gen())
        transport = _FakeTransport()
        loop.run_until_complete(resp.stream_to(transport, drain=_drain))

        # One drain per yielded chunk (not for head or terminating zero-chunk).
        assert drained == 3
        emitted = b"".join(transport.writes)
        assert b"1\r\na\r\n" in emitted
        assert emitted.endswith(b"0\r\n\r\n")
    finally:
        loop.close()


def test_streaming_response_without_drain_unchanged():
    """Omitting drain (the ASGI path) preserves the original chunk output."""
    from veloce.http.response import StreamingResponse

    loop = asyncio.new_event_loop()
    try:

        async def _gen():
            yield b"x"

        resp = StreamingResponse(_gen())
        transport = _FakeTransport()
        loop.run_until_complete(resp.stream_to(transport))

        emitted = b"".join(transport.writes)
        assert b"1\r\nx\r\n" in emitted
        assert emitted.endswith(b"0\r\n\r\n")
    finally:
        loop.close()


def test_connection_made_rejects_half_duplex_transport():
    """A write-only (half-duplex) transport is still rejected."""
    import pytest

    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        with pytest.raises(RuntimeError, match="full-duplex"):
            proto.connection_made(_WriteOnlyTransport())
    finally:
        loop.close()
