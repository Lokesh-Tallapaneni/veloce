"""StaticFiles streams large files instead of buffering the whole body."""

from __future__ import annotations

from veloce import Veloce
from veloce.contrib.staticfiles import StaticFiles
from veloce.http.request import Request
from veloce.http.response import StreamingResponse
from veloce.testclient import TestClient


async def test_handle_returns_streamingresponse_for_large_files(tmp_path):
    """Files at or above `STREAM_THRESHOLD` come back as `StreamingResponse`.

    Buffering a multi-megabyte static asset costs RSS proportional to
    the file size for the duration of the transfer; the chunked
    streaming path keeps memory bounded by `STREAM_CHUNK_SIZE`.
    """
    sf = StaticFiles(directory=str(tmp_path), prefix="/s")
    sf.STREAM_THRESHOLD = 1024  # exercise the path with a small file
    payload = b"x" * 4096
    (tmp_path / "big.bin").write_bytes(payload)

    req = Request(method="GET", path="/s/big.bin", query_string="", headers={}, body=b"")
    resp = await sf.handle(req)

    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 200
    # `Content-Length` and `Transfer-Encoding: chunked` cannot coexist
    # per RFC 9112 §6.1; chunked framing handles delivery and the
    # ETag / Last-Modified pair still lets clients reason about freshness.
    assert "Content-Length" not in resp.headers
    assert resp.headers["ETag"]


async def test_handle_returns_buffered_response_for_small_files(tmp_path):
    """Files below the threshold keep the single-message buffered path."""
    sf = StaticFiles(directory=str(tmp_path), prefix="/s")
    (tmp_path / "small.txt").write_bytes(b"hi")

    req = Request(method="GET", path="/s/small.txt", query_string="", headers={}, body=b"")
    resp = await sf.handle(req)

    assert resp is not None
    assert not isinstance(resp, StreamingResponse)
    assert resp.body == b"hi"


def test_streamed_static_file_reaches_client_intact(tmp_path):
    """End-to-end via TestClient: streamed body equals the file content."""
    app = Veloce(openapi_url=None)
    payload = b"streamed-payload" * 1024  # 16 KiB
    (tmp_path / "blob.bin").write_bytes(payload)
    sf = StaticFiles(directory=str(tmp_path), prefix="/static")
    sf.STREAM_THRESHOLD = 1024
    app._static_handlers.append(sf)

    resp = TestClient(app).get("/static/blob.bin")
    assert resp.status_code == 200
    assert resp.body == payload
