"""A streamed response revalidates too, and its 304 reports the real length.

`FileResponse` streams a file past a size threshold, so making a file large
silently changed how it caches. `ConditionalGetMiddleware` skipped the 304
downgrade for anything streamed, so every repeat request for an asset over the
threshold re-sent the whole body while the small one beside it answered 304:

    small.txt  first=200 len=   1024  revalidate=304 body=0
    big.txt    first=200 len= 204800  revalidate=200 body=204800

The skip had a real reason - `make_conditional` cleared `body` but not
`_stream`, so a downgraded streamed response would have emitted a bodiless 304
alongside its original chunks, which RFC 9110 Sec. 15.4.5 forbids. The reason
was sound and the remedy was aimed at the wrong place: the fix is to clear the
stream in the downgrade, not to refuse to downgrade.

The 304's `Content-Length` is the second half. RFC 9110 Sec. 15.4.5 says a 304
carries the header fields that would have been sent in a 200, and RFC 9111
Sec. 4.3.4 has caches write those over their stored entry - so a 304 computing
its length from the body it just emptied tells every cache that a 200 KiB asset
is zero bytes.
"""

from __future__ import annotations

import pathlib

import pytest

from veloce import ConditionalGetMiddleware, Response, Veloce
from veloce.http.response import FileResponse, StreamingResponse
from veloce.testclient import TestClient

#: Comfortably past the threshold at which `FileResponse` switches to streaming.
BIG = 200 * 1024
SMALL = 1024


@pytest.fixture
def assets(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "small.txt").write_bytes(b"s" * SMALL)
    (tmp_path / "big.txt").write_bytes(b"b" * BIG)
    return tmp_path


@pytest.fixture
def client(assets: pathlib.Path) -> TestClient:
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware())

    @app.get("/f/{name}")
    async def serve(name: str):
        return await FileResponse.from_path(str(assets / name))

    return TestClient(app)


def _revalidate(client: TestClient, name: str):
    """Fetch once, then again with the validator the first response handed out."""
    first = client.get(f"/f/{name}")
    assert first.headers.get("etag"), "no validator to revalidate with"
    return first, client.get(f"/f/{name}", headers={"If-None-Match": first.headers["etag"]})


def test_a_small_file_revalidates(client: TestClient):
    """The control: the buffered path always did this."""
    first, again = _revalidate(client, "small.txt")

    assert first.status_code == 200
    assert again.status_code == 304
    assert again.body == b""


def test_a_streamed_file_revalidates_too(client: TestClient):
    """The regression: crossing the streaming threshold disabled revalidation."""
    first, again = _revalidate(client, "big.txt")

    assert first.status_code == 200
    assert len(first.body) == BIG
    assert again.status_code == 304, "every repeat request re-sends the whole asset"


def test_the_streamed_304_carries_no_body(client: TestClient):
    """What the old skip existed to prevent, which the fix must actually deliver."""
    _first, again = _revalidate(client, "big.txt")

    assert again.body == b""


def test_the_streamed_304_reports_the_representations_length(client: TestClient):
    """A 304 claiming zero makes caches overwrite their stored length with 0."""
    _first, again = _revalidate(client, "big.txt")

    assert again.headers.get("content-length") == str(BIG)


def test_the_buffered_304_still_reports_its_length(client: TestClient):
    """The same property on the path that already held it."""
    _first, again = _revalidate(client, "small.txt")

    assert again.headers.get("content-length") == str(SMALL)


def test_a_stale_validator_still_gets_the_whole_streamed_file(client: TestClient):
    """Revalidation must stay a match test, not an unconditional 304."""
    again = client.get("/f/big.txt", headers={"If-None-Match": '"not-the-etag"'})

    assert again.status_code == 200
    assert len(again.body) == BIG


def test_a_chunked_stream_with_no_known_length_claims_none():
    """An unknown length is omitted rather than reported as zero.

    A generator-backed `StreamingResponse` has no `Content-Length` to carry
    forward. Guessing `0` is the same lie in a smaller place, so the 304 says
    nothing about the length instead.
    """
    app = Veloce(openapi_url=None)
    app.add_middleware(ConditionalGetMiddleware())

    @app.get("/gen")
    async def gen():
        async def chunks():
            yield b"one"
            yield b"two"

        response = StreamingResponse(chunks())
        response.headers["ETag"] = '"v1"'
        return response

    again = TestClient(app).get("/gen", headers={"If-None-Match": '"v1"'})

    assert again.status_code == 304
    assert again.body == b""
    assert again.headers.get("content-length") is None


def test_a_repeated_downgrade_keeps_the_first_recorded_length():
    """A handler may downgrade and the middleware downgrade again on the way out."""
    response = Response(body=b"x" * 40)
    response.headers["ETag"] = '"v1"'

    class _Req:
        if_none_match = ('"v1"',)
        if_modified_since = None

    response.make_conditional(_Req())
    response.make_conditional(_Req())

    assert response.status_code == 304
    assert response.headers["Content-Length"] == "40"


async def test_the_native_transport_writes_no_chunks_after_a_304():
    """The hazard the old skip named, on the path that actually has it.

    The ASGI emit suppresses the body for any bodiless status, so it would look
    correct even with the stream still attached. `stream_to` does not: it writes
    the head and then drains `_stream` unconditionally, which is how a 304 would
    have reached the wire with the representation's chunks behind it - the
    protocol violation (RFC 9110 Sec. 15.4.5) that made the middleware refuse to
    downgrade streams at all.
    """

    class _Transport:
        def __init__(self) -> None:
            self.written = b""

        def write(self, data: bytes) -> None:
            self.written += data

    async def chunks():
        yield b"representation-"
        yield b"bytes"

    response = StreamingResponse(chunks())
    response.headers["ETag"] = '"v1"'

    class _Req:
        if_none_match = ('"v1"',)
        if_modified_since = None

    response.make_conditional(_Req())
    assert response.status_code == 304

    transport = _Transport()
    await response.stream_to(transport, keep_alive=False)

    assert b"representation-" not in transport.written, "the 304 carried the stream's chunks"
    assert b"304" in transport.written[:32]
