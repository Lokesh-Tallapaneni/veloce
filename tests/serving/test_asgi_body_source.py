"""Pull-based ASGI body source.

`ASGIBodySource` adapts an ASGI `receive` channel to the same consumer
interface (`__aiter__` / `__anext__` / `read`) the native push-fed
`RequestBodySource` exposes, so a `Request` streams identically on either
transport. It caps the running total at `max_content_length` (413 mid-read)
and ends the stream on `http.disconnect`.
"""

from __future__ import annotations

import pytest

from veloce.exceptions import RequestEntityTooLarge
from veloce.http._body import ASGIBodySource


def _receiver(messages):
    it = iter(messages)

    async def receive():
        return next(it)

    return receive


async def test_iterates_chunks_in_order():
    recv = _receiver(
        [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd", "more_body": True},
            {"type": "http.request", "body": b"ef", "more_body": False},
        ]
    )
    chunks = [c async for c in ASGIBodySource(recv)]
    assert chunks == [b"ab", b"cd", b"ef"]


async def test_read_joins_to_full_body():
    recv = _receiver(
        [
            {"type": "http.request", "body": b"hello ", "more_body": True},
            {"type": "http.request", "body": b"world", "more_body": False},
        ]
    )
    assert await ASGIBodySource(recv).read() == b"hello world"


async def test_single_chunk_no_more_body():
    recv = _receiver([{"type": "http.request", "body": b"solo", "more_body": False}])
    assert await ASGIBodySource(recv).read() == b"solo"


async def test_empty_body():
    recv = _receiver([{"type": "http.request", "body": b"", "more_body": False}])
    assert await ASGIBodySource(recv).read() == b""


async def test_max_content_length_raises_mid_read():
    recv = _receiver(
        [
            {"type": "http.request", "body": b"aaaa", "more_body": True},
            {"type": "http.request", "body": b"bbbb", "more_body": True},
            {"type": "http.request", "body": b"cccc", "more_body": False},
        ]
    )
    src = ASGIBodySource(recv, max_content_length=6)
    with pytest.raises(RequestEntityTooLarge):
        await src.read()


async def test_disconnect_ends_stream():
    recv = _receiver(
        [
            {"type": "http.request", "body": b"part", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    assert await ASGIBodySource(recv).read() == b"part"
