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
