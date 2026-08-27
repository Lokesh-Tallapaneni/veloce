"""GZipMiddleware must compress streaming bodies chunk-by-chunk with valid framing.

Streaming responses carry no materialised ``response.body``, so the buffered
``minimum_size`` path does not apply. The middleware wraps ``response._stream``
in a single ``zlib.compressobj`` (gzip framing) and emits the gzip trailer on
the final flush, while leaving latency-sensitive streams (SSE) untouched.
"""

from __future__ import annotations

import gzip
import zlib

from tests._asgi_drive import drive
from veloce import EventSourceResponse, GZipMiddleware, Request, Veloce
from veloce.http.response import StreamingResponse


async def _drive(app: Veloce, headers: list[tuple[bytes, bytes]]):
    """Run one GET / through the ASGI app and capture the emitted messages."""
    return await drive(app, headers=headers)


def _start_headers(messages: list[dict]) -> list[tuple[bytes, bytes]]:
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["headers"]


def _body(messages: list[dict]) -> bytes:
    return b"".join(
        m["body"] for m in messages if m["type"] == "http.response.body" and m.get("body")
    )


# 50 distinct compressible chunks; large enough that gzip wins on the wire.
_CHUNKS = [b"x" * 200 for _ in range(50)]
_PLAINTEXT = b"".join(_CHUNKS)


def _make_app(content_type: str = "application/json") -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=0))

    @app.get("/")
    async def root(request: Request):
        async def gen():
            for chunk in _CHUNKS:
                yield chunk

        return StreamingResponse(gen(), content_type=content_type)

    return app


async def test_streaming_response_is_gzip_compressed():
    app = _make_app()
    messages = await _drive(app, [(b"accept-encoding", b"gzip")])

    headers = _start_headers(messages)
    lowered = [(k.lower(), v) for k, v in headers]
    keys = [k for k, _ in lowered]

    # Content-Encoding: gzip is added, no Content-Length is emitted for a stream.
    assert (b"content-encoding", b"gzip") in lowered
    assert b"content-length" not in keys

    # Vary advertises Accept-Encoding so caches key on it.
    vary = next(v for k, v in lowered if k == b"vary")
    assert b"accept-encoding" in vary.lower()

    # The concatenated chunks form one valid gzip member decoding to the original.
    body = _body(messages)
    assert gzip.decompress(body) == _PLAINTEXT


async def test_streaming_no_accept_encoding_uncompressed():
    app = _make_app()
    messages = await _drive(app, [])

    headers = _start_headers(messages)
    keys = [k.lower() for k, _ in headers]

    assert b"content-encoding" not in keys
    assert _body(messages) == _PLAINTEXT


async def test_sse_not_compressed():
    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=0))

    @app.get("/")
    async def events(request: Request):
        async def gen():
            for i in range(5):
                yield f"data: event {i}\n\n"

        return EventSourceResponse(gen())

    messages = await _drive(app, [(b"accept-encoding", b"gzip")])

    headers = _start_headers(messages)
    keys = [k.lower() for k, _ in headers]

    # SSE latency guard: never wrap an event stream in the compressor.
    assert b"content-encoding" not in keys
    body = _body(messages)
    assert b"data: event 0" in body


async def test_text_event_stream_content_type_not_compressed():
    # A bare StreamingResponse with the SSE content type is also guarded, even
    # though it is not an EventSourceResponse instance.
    app = _make_app(content_type="text/event-stream")
    messages = await _drive(app, [(b"accept-encoding", b"gzip")])

    headers = _start_headers(messages)
    keys = [k.lower() for k, _ in headers]
    assert b"content-encoding" not in keys
    assert _body(messages) == _PLAINTEXT


async def test_streaming_already_encoded_passthrough():
    app = Veloce(openapi_url=None)
    app.add_middleware(GZipMiddleware(minimum_size=0))

    # Pre-compress the payload and declare Content-Encoding on the response so
    # the middleware must not double-encode.
    pre = gzip.compress(_PLAINTEXT)

    @app.get("/")
    async def root(request: Request):
        async def gen():
            yield pre

        return StreamingResponse(
            gen(),
            content_type="application/json",
            headers={"Content-Encoding": "gzip"},
        )

    messages = await _drive(app, [(b"accept-encoding", b"gzip")])

    headers = _start_headers(messages)
    lowered = [(k.lower(), v) for k, v in headers]
    encodings = [v for k, v in lowered if k == b"content-encoding"]

    # Exactly one gzip encoding, and the bytes are the untouched pre-gzip body.
    assert encodings == [b"gzip"]
    assert _body(messages) == pre


async def test_streaming_delivers_decodable_frame_per_chunk():
    # zlib buffers internally, so without a per-chunk Z_SYNC_FLUSH a long-lived
    # chunked stream would emit only the gzip header until EOF. Drive the
    # compressor directly and assert each yielded output advances a streaming
    # decompressor by the corresponding plaintext chunk - i.e. data is
    # deliverable before the final Z_FINISH trailer is written.
    request = Request(
        method="GET",
        path="/",
        query_string="",
        headers=[(b"accept-encoding", b"gzip")],
        body=b"",
    )

    chunks = [b'{"line": %d}\n' % i for i in range(8)]

    async def gen():
        for chunk in chunks:
            yield chunk

    # `application/json` is in the default compressible set; a chunked JSON
    # stream is the canonical NDJSON-style case this fix targets.
    response = StreamingResponse(gen(), content_type="application/json")
    mw = GZipMiddleware(minimum_size=0)
    response = await mw.process_response(request, response)

    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    delivered = bytearray()
    recovered_after_each_frame: list[bytes] = []
    async for out in response._stream:
        assert out, "every yielded frame must carry bytes"
        delivered += decompressor.decompress(out)
        recovered_after_each_frame.append(bytes(delivered))

    # Recorded rather than asserted inside the loop: the check used to sit
    # behind `if frames <= len(chunks)`, so a middleware that coalesced
    # everything into one frame ran the loop once, satisfied the guard, and
    # passed - the outcome the test exists to rule out.
    assert len(recovered_after_each_frame) > len(chunks) // 2, (
        f"{len(recovered_after_each_frame)} frames for {len(chunks)} chunks - "
        "the stream was coalesced rather than delivered incrementally"
    )
    for index, recovered in enumerate(recovered_after_each_frame[: len(chunks)], start=1):
        assert recovered == b"".join(chunks[:index]), (
            f"after frame {index} the recoverable plaintext was {recovered!r}"
        )

    # The final frame carries the gzip trailer; the full plaintext round-trips.
    assert delivered == b"".join(chunks)


async def test_streaming_206_short_circuit_sets_vary():
    # A streamed 206 / Content-Range is passed through uncompressed, but the
    # middleware still advertises Vary: Accept-Encoding (RFC 9110 Sec. 12.5.5)
    # so caches see the encoding dimension on the range response too.
    request = Request(
        method="GET",
        path="/",
        query_string="",
        headers=[(b"accept-encoding", b"gzip")],
        body=b"",
    )

    async def gen():
        for chunk in _CHUNKS:
            yield chunk

    response = StreamingResponse(
        gen(),
        status_code=206,
        content_type="application/json",
        headers={"Content-Range": "bytes 0-9999/99999"},
    )
    mw = GZipMiddleware(minimum_size=0)
    response = await mw.process_response(request, response)

    assert "Content-Encoding" not in response.headers
    assert "accept-encoding" in response.headers.get("Vary", "").lower()


async def test_streaming_chunked_native_gzip():
    # Drive the native chunked path: process_response wraps _stream, then
    # StreamingResponse.stream_to frames the already-gzipped bytes as chunks.
    request = Request(
        method="GET",
        path="/",
        query_string="",
        headers=[(b"accept-encoding", b"gzip")],
        body=b"",
    )

    async def gen():
        for chunk in _CHUNKS:
            yield chunk

    response = StreamingResponse(gen(), content_type="application/json")
    mw = GZipMiddleware(minimum_size=0)
    response = await mw.process_response(request, response)

    class _Transport:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.writes.append(data)

    transport = _Transport()
    await response.stream_to(transport)

    raw = b"".join(transport.writes)
    # Strip the response head (ends at the first blank line) and dechunk.
    _, _, framed = raw.partition(b"\r\n\r\n")
    compressed = bytearray()
    while framed:
        size_line, _, rest = framed.partition(b"\r\n")
        size = int(size_line, 16)
        if size == 0:
            break
        compressed += rest[:size]
        framed = rest[size + 2 :]  # skip chunk data + trailing CRLF

    assert gzip.decompress(bytes(compressed)) == _PLAINTEXT
