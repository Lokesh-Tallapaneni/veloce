"""Response.media_type alias for content_type."""

from __future__ import annotations

from veloce import JSONResponse, Response


def test_media_type_reads_content_type():
    r = Response(body=b"hi", content_type="text/html")
    assert r.media_type == "text/html"
    assert r.media_type == r.content_type


def test_media_type_set_propagates_to_content_type():
    r = Response(body=b"hi", content_type="text/html")
    r.media_type = "application/xml"
    assert r.content_type == "application/xml"
    assert r.media_type == "application/xml"


def test_media_type_invalidates_encoded_cache():
    """Setting media_type clears any cached HTTP/1.1 encode so the new
    content type appears in the next emission."""
    r = Response(body=b"x", content_type="text/plain")
    _ = r.encode()
    assert r._encoded is not None
    r.media_type = "application/json"
    assert r._encoded is None
    # Next encode includes the new media type.
    assert b"Content-Type: application/json" in r.encode()


def test_media_type_on_jsonresponse_subclass():
    r = JSONResponse({"a": 1})
    assert r.media_type == "application/json"
    r.media_type = "application/vnd.example+json"
    assert r.content_type == "application/vnd.example+json"


def test_construction_via_content_type_kwarg_still_works():
    """The canonical ctor still uses `content_type=`; existing call sites unchanged."""
    r = Response(body=b"hi", content_type="text/css")
    assert r.media_type == "text/css"
