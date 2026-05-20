"""Response.iter_chunked(size)."""

from __future__ import annotations

import pytest

from veloce import Response
from veloce.http.response import StreamingResponse


def test_chunks_buffered_body_evenly():
    resp = Response(body=b"abcdefghij")
    assert list(resp.iter_chunked(2)) == [b"ab", b"cd", b"ef", b"gh", b"ij"]


def test_chunks_buffered_body_with_remainder():
    resp = Response(body=b"abcdefghij")
    assert list(resp.iter_chunked(3)) == [b"abc", b"def", b"ghi", b"j"]


def test_chunk_size_larger_than_body_yields_single_chunk():
    resp = Response(body=b"short")
    assert list(resp.iter_chunked(100)) == [b"short"]


def test_empty_body_yields_no_chunks():
    resp = Response()
    assert list(resp.iter_chunked(8)) == []


def test_size_zero_raises():
    with pytest.raises(ValueError, match="positive"):
        Response(body=b"x").iter_chunked(0)


def test_negative_size_raises():
    with pytest.raises(ValueError, match="positive"):
        Response(body=b"x").iter_chunked(-3)


def test_streaming_response_returns_underlying_stream():
    """Streaming bodies pass through — chunking is the producer's job."""

    async def gen():
        yield b"x"

    sr = StreamingResponse(content=gen())
    assert sr.iter_chunked(4) is sr._stream
