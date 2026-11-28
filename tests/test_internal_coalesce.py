"""`_coalesce` — batches a stream's chunks whenever the producer does not await.

`StreamingResponse`/`EventSourceResponse` write one ASGI message, or one
`transport.write`, per chunk. Fine for a producer that genuinely waits between
chunks; wasteful for a plain loop with no `await` between `yield`s, which is
the common case. Hand-rolled coroutine stepping is easy to get subtly wrong, so
this tests the helper directly rather than through the full response/ASGI
stack - `TestClient.body` is one blob, which would hide exactly the batching
behavior under test.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce._internal import _coalesce


async def _collect(stream, flush_at=65536):
    return [chunk async for chunk in _coalesce(stream, flush_at=flush_at)]


async def test_chunks_produced_without_awaiting_are_merged():
    async def gen():
        for i in range(5):
            yield str(i).encode()

    assert await _collect(gen()) == [b"01234"]


async def test_a_real_await_between_chunks_starts_a_new_batch():
    async def gen():
        yield b"a"
        await asyncio.sleep(0)
        yield b"b"
        await asyncio.sleep(0)
        yield b"c"

    assert await _collect(gen()) == [b"a", b"b", b"c"]


async def test_a_mix_of_awaiting_and_not_batches_only_the_contiguous_run():
    async def gen():
        yield b"a"
        yield b"b"  # no await before this: merges with "a"
        await asyncio.sleep(0)
        yield b"c"
        yield b"d"  # merges with "c"

    assert await _collect(gen()) == [b"ab", b"cd"]


async def test_an_await_chain_several_frames_deep_still_flushes_once():
    async def inner():
        await asyncio.sleep(0)

    async def gen():
        yield b"a"
        await inner()
        yield b"b"

    assert await _collect(gen()) == [b"a", b"b"]


async def test_a_burst_splits_at_flush_at():
    async def gen():
        for _ in range(10):
            yield b"x" * 10

    chunks = await _collect(gen(), flush_at=25)
    assert b"".join(chunks) == b"x" * 100
    assert len(chunks) > 1
    assert all(len(c) <= 30 for c in chunks)


async def test_str_chunks_are_encoded_exactly_as_before():
    async def gen():
        yield "héllo"
        yield " world"

    assert await _collect(gen()) == ["héllo world".encode()]


async def test_an_empty_stream_yields_nothing():
    async def gen():
        return
        yield b"unreachable"  # pragma: no cover

    assert await _collect(gen()) == []


async def test_a_single_chunk_is_flushed_at_the_end():
    async def gen():
        yield b"only"

    assert await _collect(gen()) == [b"only"]


async def test_a_producer_that_raises_after_n_chunks_still_delivers_them():
    class Boom(Exception):
        pass

    async def gen():
        yield b"a"
        yield b"b"
        yield b"c"
        raise Boom("stop")

    delivered = []
    with pytest.raises(Boom):
        async for chunk in _coalesce(gen()):
            delivered.append(chunk)
    assert b"".join(delivered) == b"abc"


async def test_a_producer_that_raises_after_awaiting_still_flushes_first():
    class Boom(Exception):
        pass

    async def gen():
        yield b"a"
        await asyncio.sleep(0)
        raise Boom("stop")

    delivered = []
    with pytest.raises(Boom):
        async for chunk in _coalesce(gen()):
            delivered.append(chunk)
    assert delivered == [b"a"]


async def test_closing_the_consumer_runs_the_producers_finally():
    ran_finally = []

    async def gen():
        try:
            yield b"a"
            await asyncio.sleep(10)
            yield b"b"  # pragma: no cover - the consumer closes first
        finally:
            ran_finally.append(True)

    agen = _coalesce(gen())
    first = await agen.__anext__()
    assert first == b"a"
    await agen.aclose()
    assert ran_finally == [True]


async def test_cancelling_a_pending_chunk_runs_the_producers_finally():
    """Task cancellation lands mid-await, not at a bare `yield` - a different
    path through the helper than closing the consumer between chunks does."""
    ran_finally = []

    async def gen():
        try:
            yield b"a"
            await asyncio.sleep(10)
            yield b"b"  # pragma: no cover - cancelled first
        finally:
            ran_finally.append(True)

    agen = _coalesce(gen())
    first = await agen.__anext__()
    assert first == b"a"

    task = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ran_finally == [True]
