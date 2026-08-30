"""A streamed `FileResponse` frames its body consistently with its own head.

`FileResponse.stream_to` writes the buffered head and then the file's chunks
raw, which is correct only while the head carries the `Content-Length`
`from_path` set. A response middleware may legitimately remove that header -
`CompressionMiddleware` does, for every streamed body, because a compressed
length is not the length that was stat'd - and `encode()` then falls back to
`len(self.body)`, which is `0` for a streamed response.

The result was a head advertising `Content-Length: 0` followed by the body
bytes. On a keep-alive connection the client reads a zero-length body and then
parses those bytes as the start of the next response.

These tests hold the invariant rather than the mechanism: whatever the head
says about framing, the bytes on the wire must agree with it.
"""

from __future__ import annotations

import gzip

import pytest

from tests._protocol import _FakeTransport
from veloce import GZipMiddleware, Veloce
from veloce.helpers import async_send_file
from veloce.http.response import _INLINE_READ_MAX

#: Comfortably over the inline-read cutoff, so `from_path` streams it.
BIG = b"veloce " * 40000


def _framing(emitted: bytes) -> tuple[int | None, bool, int]:
    """`(declared length, chunked, body byte count)` from a raw response."""
    head, _, body = emitted.partition(b"\r\n\r\n")
    declared: int | None = None
    chunked = False
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            declared = int(value.strip())
        elif name.strip().lower() == b"transfer-encoding":
            chunked = b"chunked" in value.strip().lower()
    return declared, chunked, len(body)


async def _serve(tmp_path, *middleware) -> bytes:
    """Drive one GET for a large file through the native protocol."""
    import asyncio

    from veloce.serving.protocol import HttpProtocol

    path = tmp_path / "big.txt"
    path.write_bytes(BIG)

    app = Veloce(openapi_url=None)
    for mw in middleware:
        app.add_middleware(mw)

    @app.get("/f")
    async def f():
        return await async_send_file(str(path))

    loop = asyncio.get_running_loop()
    proto = HttpProtocol(app, loop)
    transport = _FakeTransport()
    proto.connection_made(transport)
    proto.data_received(b"GET /f HTTP/1.1\r\nHost: t\r\nAccept-Encoding: gzip\r\n\r\n")

    # Drive until the emitted byte count stops growing. A break on "the head is
    # written" would stop before the body chunks, which is the half these tests
    # are about.
    # `_stream_file` reads in a thread-pool executor, so the body cannot arrive
    # by yielding the loop alone - the result has to be delivered, which takes
    # real time. Wait for the byte count to stop growing across a short wall
    # clock window, bounded so a body that never arrives fails the assertion
    # rather than hanging.
    settled, quiet = 0, 0
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.002)
        size = sum(len(chunk) for chunk in transport.writes)
        quiet = quiet + 1 if size == settled else 0
        settled = size
        if size and quiet > 25:
            break
    return b"".join(transport.writes)


async def test_a_large_file_is_length_delimited_and_the_length_is_right(tmp_path):
    """The fast path: the length is known, so the body is length-delimited."""
    declared, chunked, sent = _framing(await _serve(tmp_path))

    assert not chunked, "an uncompressed file of known size needs no chunked framing"
    assert declared == len(BIG)
    assert sent == declared, f"head declared {declared}, wire carried {sent}"


async def test_a_compressed_large_file_frames_what_it_sends(tmp_path):
    """The regression: compression removes `Content-Length` for a streamed body.

    Whatever the head then says, it has to be true. Either the response is
    chunk-framed, or it declares the number of bytes that follow - never
    `Content-Length: 0` with a body.
    """
    emitted = await _serve(tmp_path, GZipMiddleware(minimum_size=0))
    declared, chunked, sent = _framing(emitted)

    assert sent > 0, "the compressed body was not sent at all"
    if chunked:
        assert declared is None, "a chunked response must not also declare a length"
    else:
        assert declared == sent, (
            f"head declared Content-Length: {declared} and then wrote {sent} bytes - "
            "a client reads the surplus as the start of the next response"
        )


async def test_the_compressed_body_is_the_file(tmp_path):
    """Framing is not enough: the bytes have to be the file, still."""
    emitted = await _serve(tmp_path, GZipMiddleware(minimum_size=0))
    _declared, chunked, _sent = _framing(emitted)
    _head, _, body = emitted.partition(b"\r\n\r\n")

    if chunked:
        payload, rest = b"", body
        while rest:
            size_line, _, rest = rest.partition(b"\r\n")
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                break
            payload += rest[:size]
            rest = rest[size + 2 :]
    else:
        payload = body

    assert gzip.decompress(payload) == BIG


@pytest.mark.parametrize("compress", [False, True], ids=["plain", "gzip"])
async def test_a_head_request_sends_no_body_either_way(tmp_path, compress):
    """The bodiless-status rule must survive the fix."""
    import asyncio

    from veloce.serving.protocol import HttpProtocol

    path = tmp_path / "big.txt"
    path.write_bytes(BIG)
    app = Veloce(openapi_url=None)
    if compress:
        app.add_middleware(GZipMiddleware(minimum_size=0))

    @app.get("/f")
    async def f():
        return await async_send_file(str(path))

    loop = asyncio.get_running_loop()
    proto = HttpProtocol(app, loop)
    transport = _FakeTransport()
    proto.connection_made(transport)
    proto.data_received(b"HEAD /f HTTP/1.1\r\nHost: t\r\nAccept-Encoding: gzip\r\n\r\n")
    for _ in range(600):
        await asyncio.sleep(0)

    emitted = b"".join(transport.writes)
    _head, _, body = emitted.partition(b"\r\n\r\n")
    assert body == b"", "a HEAD response carries no body"


def test_the_threshold_this_rests_on_is_still_the_one_measured():
    """These tests only exercise the bug above `_INLINE_READ_MAX`."""
    assert len(BIG) > _INLINE_READ_MAX
