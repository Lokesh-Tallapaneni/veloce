"""Response.charset setter."""

from __future__ import annotations

from veloce import Response


def test_charset_default_utf8():
    assert Response().charset == "utf-8"


def test_charset_setter_rewrites_content_type():
    resp = Response(content_type="text/html")
    resp.charset = "iso-8859-1"
    assert resp.charset == "iso-8859-1"
    assert "charset=iso-8859-1" in resp.content_type
    assert resp.content_type.startswith("text/html")


def test_charset_setter_replaces_existing_charset():
    resp = Response(content_type="text/html; charset=utf-8")
    resp.charset = "windows-1252"
    assert resp.charset == "windows-1252"
    # Old charset is gone.
    assert "utf-8" not in resp.content_type


def test_charset_setter_preserves_media_type():
    resp = Response(content_type="application/json; charset=utf-8")
    resp.charset = "ascii"
    assert resp.content_type.split(";")[0].strip() == "application/json"


def test_charset_setter_on_default_content_type():
    resp = Response()
    resp.charset = "utf-16"
    assert "charset=utf-16" in resp.content_type
