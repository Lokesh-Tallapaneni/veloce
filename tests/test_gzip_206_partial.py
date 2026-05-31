"""GZipMiddleware must not compress partial-content (206) responses."""

from __future__ import annotations

import pytest

from veloce import Request, Response
from veloce.middleware.compression import GZipMiddleware


def _req() -> Request:
    return Request(
        method="GET",
        path="/f",
        query_string="",
        headers={"Accept-Encoding": "gzip"},
        body=b"",
    )


@pytest.mark.asyncio
async def test_gzip_skips_206_with_content_range():
    """A 206 response carrying Content-Range passes through uncompressed."""
    mw = GZipMiddleware(minimum_size=10)
    body = b'{"data":"' + b"a" * 2000 + b'"}'
    resp = Response(
        status_code=206,
        body=body,
        content_type="application/json",
        headers={"Content-Range": "bytes 0-2010/9999", "Accept-Ranges": "bytes"},
    )
    out = await mw.process_response(_req(), resp)
    assert out.body == body
    assert "Content-Encoding" not in out.headers
    assert out.headers["Content-Range"] == "bytes 0-2010/9999"


@pytest.mark.asyncio
async def test_gzip_still_compresses_plain_200():
    """A normal 200 with a compressible body is still gzipped (guard is narrow)."""
    mw = GZipMiddleware(minimum_size=10)
    body = b'{"data":"' + b"a" * 2000 + b'"}'
    resp = Response(status_code=200, body=body, content_type="application/json")
    out = await mw.process_response(_req(), resp)
    assert out.headers.get("Content-Encoding") == "gzip"
    assert len(out.body) < len(body)
