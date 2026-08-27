"""A large file is served without being held in memory.

`FileResponse` read the whole file into `body`, so resident memory scaled with
(file size x concurrent requests): four concurrent 32 MiB downloads measured at
134 MB RSS. That needs no malice to hurt - it is the ordinary behaviour of
serving large files.

A file above the inline threshold is now streamed off disk in executor-read
chunks. Its length is known from the stat, so the response stays
length-delimited: this streams a known-size body rather than switching to
chunked encoding, and a client still learns the size up front.

A small file keeps the inline read. The thread-pool hop dominates serving a
small asset, so the fast path is deliberately unchanged.
"""

from __future__ import annotations

import asyncio
import tracemalloc
from pathlib import Path

import pytest

from veloce import Veloce
from veloce.http.response import _INLINE_READ_MAX, FileResponse

_LARGE = _INLINE_READ_MAX * 64  # 4 MiB, comfortably above the threshold


@pytest.fixture
def large_file(tmp_path):
    path = tmp_path / "large.bin"
    path.write_bytes(b"F" * _LARGE)
    return str(path)


@pytest.fixture
def small_file(tmp_path):
    path = tmp_path / "small.bin"
    path.write_bytes(b"s" * 1024)
    return str(path)


async def _serve(path: str) -> tuple[dict, bytes]:
    """Drive one ASGI request that serves `path`, returning (headers, body)."""
    app = Veloce(openapi_url=None)

    @app.get("/f")
    async def serve():
        return await FileResponse.from_path(path)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/f",
        "query_string": b"",
        "headers": [],
        "root_path": "",
        "scheme": "http",
        "http_version": "1.1",
    }
    out: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        out.append(message)

    await app(scope, receive, send)
    headers = {k.decode().lower(): v.decode() for k, v in out[0]["headers"]}
    return headers, b"".join(m.get("body", b"") for m in out[1:])


# ── The bytes still arrive, and the framing still describes them ─────


async def test_a_large_file_is_served_whole(large_file):
    _headers, body = await _serve(large_file)
    assert body == b"F" * _LARGE


async def test_a_large_file_still_advertises_its_length(large_file):
    """Streaming must not silently turn a sized download into a chunked one."""
    headers, _body = await _serve(large_file)
    assert headers["content-length"] == str(_LARGE)
    assert "transfer-encoding" not in headers


async def test_a_small_file_is_served_whole(small_file):
    _headers, body = await _serve(small_file)
    assert body == b"s" * 1024


# ── ...without holding the file ──────────────────────────────────────


async def test_a_large_file_is_not_held_in_memory(large_file):
    """The defect: RSS scaled with file size times concurrent requests.

    The receiver counts bytes rather than joining them - accumulating the body
    here would measure the test's own retention, not the framework's.
    """
    app = Veloce(openapi_url=None)

    @app.get("/f")
    async def serve():
        return await FileResponse.from_path(large_file)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/f",
        "query_string": b"",
        "headers": [],
        "root_path": "",
        "scheme": "http",
        "http_version": "1.1",
    }
    served = 0

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        nonlocal served
        served += len(message.get("body", b""))

    tracemalloc.start()
    try:
        await app(scope, receive, send)
        retained, _peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert served == _LARGE
    # Retention must not scale with the file: a few chunks, not the whole thing.
    assert retained < _LARGE // 4, f"retained {retained / 1e6:.2f} MB of a 4 MiB file"


async def test_a_large_file_response_carries_no_buffered_body(large_file):
    response = await FileResponse.from_path(large_file)
    assert response.is_streamed
    assert response.body == b""


async def test_a_small_file_keeps_the_inline_read(small_file):
    """The fast path for a small asset is deliberately unchanged."""
    response = await FileResponse.from_path(small_file)
    assert not response.is_streamed
    assert response.body == b"s" * 1024


# ── The file handle does not outlive the response ────────────────────


async def test_streaming_closes_the_file(large_file, tmp_path):
    """A leaked handle would keep the file locked and exhaust descriptors."""
    response = await FileResponse.from_path(large_file)
    consumed = b"".join([chunk async for chunk in response._stream])
    assert len(consumed) == _LARGE
    # On Windows an open handle blocks removal outright, which makes this a
    # direct test rather than a proxy for one. Offloaded so the filesystem call
    # does not run on the loop.
    target = Path(large_file)
    await asyncio.to_thread(target.unlink)
    assert not await asyncio.to_thread(target.exists)
