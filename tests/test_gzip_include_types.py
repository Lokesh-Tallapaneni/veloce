"""GZipMiddleware include_types / exclude_types filtering."""

from __future__ import annotations

import gzip

from tests.conftest import make_request
from veloce import GZipMiddleware, Request, Response


def _req() -> Request:
    return make_request(
        method="GET",
        path="/x",
        query_string="",
        headers={"accept-encoding": "gzip"},
        body=b"",
    )


async def test_compresses_text_content_type():
    mw = GZipMiddleware(minimum_size=0)
    resp = Response(
        body=b"x" * 5000,
        content_type="text/html",
    )
    out = await mw.process_response(_req(), resp)
    assert out.headers.get("Content-Encoding") == "gzip"
    assert gzip.decompress(out.body) == b"x" * 5000


async def test_compresses_application_json():
    mw = GZipMiddleware(minimum_size=0)
    resp = Response(
        body=b"x" * 5000,
        content_type="application/json",
    )
    out = await mw.process_response(_req(), resp)
    assert out.headers.get("Content-Encoding") == "gzip"


async def test_skips_image_jpeg_by_default():
    mw = GZipMiddleware(minimum_size=0)
    resp = Response(
        body=b"x" * 5000,
        content_type="image/jpeg",
    )
    out = await mw.process_response(_req(), resp)
    # Not in the default compressible set → bypassed.
    assert "Content-Encoding" not in out.headers


async def test_skips_zip_by_default():
    mw = GZipMiddleware(minimum_size=0)
    resp = Response(body=b"x" * 5000, content_type="application/zip")
    out = await mw.process_response(_req(), resp)
    assert "Content-Encoding" not in out.headers


async def test_custom_include_types_overrides_default():
    mw = GZipMiddleware(minimum_size=0, include_types=("application/octet-stream",))
    resp = Response(body=b"x" * 5000, content_type="application/octet-stream")
    out = await mw.process_response(_req(), resp)
    assert out.headers.get("Content-Encoding") == "gzip"


async def test_exclude_types_blocks_otherwise_compressible():
    """A type in both lists ends up excluded — exclude wins."""
    mw = GZipMiddleware(minimum_size=0, exclude_types=("text/event-stream",))
    resp = Response(body=b"x" * 5000, content_type="text/event-stream")
    out = await mw.process_response(_req(), resp)
    assert "Content-Encoding" not in out.headers


async def test_compresslevel_passed_through():
    """Higher compresslevel should produce smaller output than default."""
    payload = b"abcdef" * 1000
    mw_low = GZipMiddleware(minimum_size=0, compresslevel=1)
    mw_high = GZipMiddleware(minimum_size=0, compresslevel=9)
    low = (
        await mw_low.process_response(_req(), Response(body=payload, content_type="text/plain"))
    ).body
    high = (
        await mw_high.process_response(_req(), Response(body=payload, content_type="text/plain"))
    ).body
    # Strictly smaller, not `<=`: for this repeating payload level 1 gives 68
    # bytes and level 9 gives 49, so `<=` would also hold if `compresslevel`
    # were ignored entirely and both emitted identical bytes.
    assert len(high) < len(low)
