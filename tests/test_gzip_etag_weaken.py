"""GZipMiddleware weakens a strong ETag after compressing the body.

Compression rewrites the bytes on the wire, so a strong validator (RFC 9110
Sec. 8.8.1 - byte-identical representations) no longer describes them. The
middleware downgrades a handler-set strong ETag to `W/...`; weak, absent, or
malformed tags are left untouched.
"""

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


def _big_json() -> bytes:
    return b'{"data":"' + b"a" * 2000 + b'"}'


@pytest.mark.asyncio
async def test_strong_etag_weakened_after_gzip():
    mw = GZipMiddleware(minimum_size=10)
    resp = Response(status_code=200, body=_big_json(), content_type="application/json")
    original = resp.add_etag()  # strong, e.g. '"<md5>"'
    out = await mw.process_response(_req(), resp)
    assert out.headers["Content-Encoding"] == "gzip"
    assert out.headers["ETag"] == "W/" + original


@pytest.mark.asyncio
async def test_weak_etag_not_double_weakened():
    mw = GZipMiddleware(minimum_size=10)
    resp = Response(status_code=200, body=_big_json(), content_type="application/json")
    weak = resp.add_etag(weak=True)  # 'W/"<md5>"'
    out = await mw.process_response(_req(), resp)
    assert out.headers["Content-Encoding"] == "gzip"
    assert out.headers["ETag"] == weak  # unchanged, no W/W/


@pytest.mark.asyncio
async def test_no_etag_not_fabricated():
    mw = GZipMiddleware(minimum_size=10)
    resp = Response(status_code=200, body=_big_json(), content_type="application/json")
    out = await mw.process_response(_req(), resp)
    assert out.headers["Content-Encoding"] == "gzip"
    assert "ETag" not in out.headers


@pytest.mark.asyncio
async def test_strong_etag_left_strong_when_not_compressed():
    # Body below minimum_size: compression is skipped, ETag stays strong.
    mw = GZipMiddleware(minimum_size=10_000)
    resp = Response(status_code=200, body=_big_json(), content_type="application/json")
    original = resp.add_etag()
    out = await mw.process_response(_req(), resp)
    assert "Content-Encoding" not in out.headers
    assert out.headers["ETag"] == original


@pytest.mark.asyncio
async def test_lowercase_etag_key_is_weakened():
    mw = GZipMiddleware(minimum_size=10)
    resp = Response(status_code=200, body=_big_json(), content_type="application/json")
    resp.headers["etag"] = '"deadbeef"'  # lowercase spelling
    out = await mw.process_response(_req(), resp)
    assert out.headers["Content-Encoding"] == "gzip"
    assert out.headers["etag"] == 'W/"deadbeef"'


@pytest.mark.asyncio
async def test_mixedcase_etag_key_is_weakened():
    # Field names are case-insensitive (RFC 9110 Sec. 5.1): a handler-set
    # `Etag` (any casing) must be located and weakened in place under that
    # same key after the wire bytes change.
    mw = GZipMiddleware(minimum_size=10)
    resp = Response(status_code=200, body=_big_json(), content_type="application/json")
    resp.headers["Etag"] = '"deadbeef"'  # mixed-case spelling
    out = await mw.process_response(_req(), resp)
    assert out.headers["Content-Encoding"] == "gzip"
    assert out.headers["Etag"] == 'W/"deadbeef"'
    # No duplicate canonical key was introduced.
    assert "ETag" not in out.headers


@pytest.mark.asyncio
async def test_malformed_unquoted_etag_left_untouched():
    mw = GZipMiddleware(minimum_size=10)
    resp = Response(status_code=200, body=_big_json(), content_type="application/json")
    resp.headers["ETag"] = "not-quoted"
    out = await mw.process_response(_req(), resp)
    assert out.headers["Content-Encoding"] == "gzip"
    assert out.headers["ETag"] == "not-quoted"
