"""GZipMiddleware must not compress partial-content (206) responses."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Response
from veloce.middleware.compression import GZipMiddleware


def _req() -> Request:
    return make_request(
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


def _vary(response: Response) -> str:
    """Return the response's Vary header value, lower-cased, or '' if absent."""
    return response.headers.get("Vary", "").lower()


async def test_gzip_206_short_circuit_sets_vary():
    """The 206 / Content-Range pass-through still advertises Vary: Accept-Encoding."""
    mw = GZipMiddleware(minimum_size=10)
    body = b'{"data":"' + b"a" * 2000 + b'"}'
    resp = Response(
        status_code=206,
        body=body,
        content_type="application/json",
        headers={"Content-Range": "bytes 0-2010/9999", "Accept-Ranges": "bytes"},
    )
    out = await mw.process_response(_req(), resp)
    assert "Content-Encoding" not in out.headers
    assert "accept-encoding" in _vary(out)


async def test_gzip_no_accept_encoding_sets_vary():
    """A client that does not accept gzip gets an uncompressed body that still varies."""
    mw = GZipMiddleware(minimum_size=10)
    body = b'{"data":"' + b"a" * 2000 + b'"}'
    req = Request(method="GET", path="/f", query_string="", headers={}, body=b"")
    resp = Response(status_code=200, body=body, content_type="application/json")
    out = await mw.process_response(req, resp)
    assert "Content-Encoding" not in out.headers
    assert "accept-encoding" in _vary(out)


async def test_gzip_below_minimum_size_sets_vary():
    """A body under `minimum_size` passes through uncompressed but still varies."""
    mw = GZipMiddleware(minimum_size=500)
    resp = Response(status_code=200, body=b"tiny", content_type="application/json")
    out = await mw.process_response(_req(), resp)
    assert "Content-Encoding" not in out.headers
    assert "accept-encoding" in _vary(out)


async def test_gzip_incompressible_type_sets_vary():
    """An incompressible content type is left alone but still advertises Vary."""
    mw = GZipMiddleware(minimum_size=10)
    body = b"\x89PNG\r\n" + b"\x00" * 2000
    resp = Response(status_code=200, body=body, content_type="image/png")
    out = await mw.process_response(_req(), resp)
    assert "Content-Encoding" not in out.headers
    assert "accept-encoding" in _vary(out)
