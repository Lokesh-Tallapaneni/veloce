"""Response.mimetype bare media-type accessor."""

from __future__ import annotations

from veloce import Response


def test_mimetype_strips_parameters():
    resp = Response(content_type="text/html; charset=utf-8")
    assert resp.mimetype == "text/html"


def test_mimetype_lowercased():
    resp = Response(content_type="TEXT/HTML")
    assert resp.mimetype == "text/html"


def test_mimetype_no_params():
    resp = Response(content_type="application/json")
    assert resp.mimetype == "application/json"


def test_mimetype_setter_preserves_charset():
    resp = Response(content_type="text/plain; charset=utf-8")
    resp.mimetype = "text/html"
    assert resp.mimetype == "text/html"
    assert "charset=utf-8" in resp.content_type


def test_mimetype_setter_without_existing_charset():
    resp = Response(content_type="text/plain")
    resp.mimetype = "application/xml"
    assert resp.content_type == "application/xml"
    assert "charset" not in resp.content_type


def test_mimetype_setter_then_getter_roundtrip():
    resp = Response()
    resp.mimetype = "image/png"
    assert resp.mimetype == "image/png"


def test_ct_cache_invalidates_on_direct_reassignment():
    # `content_type` is a bare public slot reassigned directly (no setter
    # hook); the value-keyed cache must reflect the new value on next access.
    resp = Response(content_type="text/html; charset=utf-8")
    assert resp.mimetype == "text/html"
    assert resp.charset == "utf-8"
    assert resp.mimetype_params == {"charset": "utf-8"}

    resp.content_type = "text/csv; charset=ascii"
    assert resp.mimetype == "text/csv"
    assert resp.charset == "ascii"
    assert resp.mimetype_params == {"charset": "ascii"}


def test_ct_cache_invalidates_when_params_disappear():
    resp = Response(content_type="application/json; charset=utf-8")
    assert resp.mimetype_params == {"charset": "utf-8"}
    assert resp.charset == "utf-8"

    resp.content_type = "application/json"
    assert resp.mimetype_params == {}
    # No charset parameter -> documented "utf-8" fallback.
    assert resp.charset == "utf-8"


def test_ct_cache_repeated_access_is_stable():
    resp = Response(content_type="text/plain; charset=latin-1")
    first = (resp.mimetype, resp.charset, resp.mimetype_params)
    second = (resp.mimetype, resp.charset, resp.mimetype_params)
    assert first == second == ("text/plain", "latin-1", {"charset": "latin-1"})
