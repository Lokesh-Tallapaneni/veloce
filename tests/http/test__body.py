"""Direct unit coverage for RequestBodySource — the async producer/consumer
buffer between the parser callbacks and a streaming handler's reads.

These exercise the overflow latch, EOF semantics, single-waiter wakeups and
drain() in isolation, complementing the protocol-level tests that drive the
source indirectly through httptools callbacks.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce.exceptions import RequestEntityTooLarge
from veloce.http._body import RequestBodySource


async def test_feed_then_read_returns_joined_bytes():
    src = RequestBodySource()
    src.feed(b"abc")
    src.feed(b"def")
    src.feed_eof()
    assert await src.read() == b"abcdef"
    assert src.total_bytes == 6


async def test_empty_feed_is_ignored():
    src = RequestBodySource()
    src.feed(b"")
    src.feed_eof()
    assert await src.read() == b""
    assert src.total_bytes == 0


async def test_at_eof_property_tracks_eof_and_drained_chunks():
    src = RequestBodySource()
    src.feed(b"x")
    # EOF not yet signalled — definitely not at_eof.
    assert src.at_eof is False
    src.feed_eof()
    # EOF signalled but a buffered chunk remains.
    assert src.at_eof is False
    assert await anext(aiter(src)) == b"x"
    # EOF signalled and all chunks consumed.
    assert src.at_eof is True


async def test_anext_raises_too_large_once_overflow_latches_and_drops_bytes():
    src = RequestBodySource(max_content_length=4)
    src.feed(b"ab")  # within limit, buffered
    src.feed(b"cde")  # crosses the 4-byte cap → overflow latch, bytes dropped
    src.feed_eof()
    it = aiter(src)
    # The in-limit chunk is still delivered first.
    assert await anext(it) == b"ab"
    # Then the overflow latch raises before any further chunk / EOF.
    with pytest.raises(RequestEntityTooLarge):
        await anext(it)
    # total_bytes reflects everything fed (including the dropped bytes), but the
    # over-limit chunk was never appended to the buffer.
    assert src.total_bytes == 5


async def test_read_raises_too_large_on_overflow():
    src = RequestBodySource(max_content_length=3)
    src.feed(b"toolong")
    src.feed_eof()
    with pytest.raises(RequestEntityTooLarge):
        await src.read()


async def test_feed_eof_makes_anext_raise_stop_async_iteration_after_draining():
    src = RequestBodySource()
    src.feed(b"only")
    src.feed_eof()
    it = aiter(src)
    assert await anext(it) == b"only"
    with pytest.raises(StopAsyncIteration):
        await anext(it)


async def test_consumer_blocked_in_anext_wakes_on_feed():
    src = RequestBodySource()
    it = aiter(src)
    task = asyncio.ensure_future(anext(it))
    # Let the consumer reach the await — nothing fed yet, so it blocks.
    await asyncio.sleep(0)
    assert not task.done()

    src.feed(b"late")
    await asyncio.sleep(0)
    assert task.done()
    assert task.result() == b"late"


async def test_consumer_blocked_in_anext_wakes_on_feed_eof():
    src = RequestBodySource()
    it = aiter(src)
    task = asyncio.ensure_future(anext(it))
    await asyncio.sleep(0)
    assert not task.done()

    src.feed_eof()
    await asyncio.sleep(0)
    assert task.done()
    with pytest.raises(StopAsyncIteration):
        task.result()


async def test_drain_with_eof_already_set_returns_immediately_and_clears_chunks():
    src = RequestBodySource()
    src.feed(b"unread")
    src.feed_eof()
    # EOF already known — drain must not block and must discard the unread chunk.
    await asyncio.wait_for(src.drain(), timeout=0.1)
    assert src.at_eof is True
    # Nothing left to read after a drain.
    assert await src.read() == b""


async def test_drain_blocks_until_eof_then_discards_streamed_bytes():
    src = RequestBodySource()
    src.feed(b"partial")
    drain_task = asyncio.ensure_future(src.drain())
    await asyncio.sleep(0)
    # EOF not signalled yet → drain is still awaiting more body.
    assert not drain_task.done()

    # More body arrives, still no EOF — drain keeps waiting.
    src.feed(b"more")
    await asyncio.sleep(0)
    assert not drain_task.done()

    # EOF unblocks the drain.
    src.feed_eof()
    await asyncio.wait_for(drain_task, timeout=0.1)
    assert src.at_eof is True


async def test_drain_resets_size_while_blocked_so_streamed_bytes_do_not_trip_cap():
    # A body-ignoring handler on an over-cap source: while drain blocks awaiting
    # EOF it discards each arriving chunk and resets the running total, so a
    # still-streaming over-limit body neither buffers nor re-raises mid-drain.
    src = RequestBodySource(max_content_length=4)
    src.feed(b"abcd")  # at the cap, buffered
    drain_task = asyncio.ensure_future(src.drain())
    await asyncio.sleep(0)
    # First loop iteration cleared the buffered chunk and reset the total.
    assert not drain_task.done()
    assert src.total_bytes == 0

    # More body keeps arriving; drain keeps discarding without raising.
    src.feed(b"efgh")
    await asyncio.sleep(0)
    assert not drain_task.done()

    src.feed_eof()
    await asyncio.wait_for(drain_task, timeout=0.1)
    assert src.at_eof is True
