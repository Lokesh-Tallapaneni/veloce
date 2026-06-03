"""GZipMiddleware must strip a stale Content-Length under any header casing.

HTTP field names are case-insensitive (RFC 9110 Sec. 5.1), so a handler may
return ``Content-length`` (or any other spelling). After gzip the declared
length describes the *uncompressed* representation and is stale; the middleware
must drop every casing and leave exactly one canonical ``Content-Length`` (the
buffered path) or none at all (the chunked streaming path), plus exactly one
``Content-Encoding: gzip``.
"""

from __future__ import annotations

import gzip

from veloce import GZipMiddleware, Request
from veloce.http.response import JSONResponse, StreamingResponse, header_key


def _make_request() -> Request:
    return Request(
        method="GET",
        path="/",
        query_string="",
        headers={"accept-encoding": "gzip"},
        body=b"",
    )


async def test_buffered_strips_stale_mixed_case_content_length():
    mw = GZipMiddleware(minimum_size=0)
    request = _make_request()
    # Handler returns a stale uncompressed length under a non-canonical casing.
    response = JSONResponse({"value": "x" * 5000}, headers={"Content-length": "99999"})

    result = await mw.process_response(request, response)

    # Exactly one Content-Length under any casing, equal to the compressed body.
    cl_keys = [k for k in result.headers if k.lower() == "content-length"]
    assert len(cl_keys) == 1
    assert int(result.headers[cl_keys[0]]) == len(result.body)
    # Stale mixed-case value is gone.
    assert "99999" not in result.headers.values()
    # Exactly one Content-Encoding: gzip.
    ce_keys = [k for k in result.headers if k.lower() == "content-encoding"]
    assert len(ce_keys) == 1
    assert result.headers[ce_keys[0]] == "gzip"
    assert gzip.decompress(result.body)


async def test_streaming_strips_stale_mixed_case_content_length():
    mw = GZipMiddleware(minimum_size=0)
    request = _make_request()
    response = StreamingResponse(
        (b"chunk" * 200 for _ in range(10)),
        content_type="text/plain",
        headers={"Content-length": "2000", "Content-Encoding": "identity"},
    )

    result = await mw.process_response(request, response)

    # No Content-Length survives on the chunked path (any casing).
    assert header_key(result.headers, "content-length") is None
    # Exactly one Content-Encoding: gzip (the pre-existing identity is gone).
    ce_keys = [k for k in result.headers if k.lower() == "content-encoding"]
    assert len(ce_keys) == 1
    assert result.headers[ce_keys[0]] == "gzip"
