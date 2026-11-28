"""Response.content_encoding / content_language — typed header accessors."""

from __future__ import annotations

from veloce import Response

# ── content_encoding ────────────────────────────────────────────────


def test_content_encoding_none_by_default():
    assert Response().content_encoding is None


def test_content_encoding_set_and_read():
    resp = Response()
    resp.content_encoding = "gzip"
    assert resp.headers["Content-Encoding"] == "gzip"
    assert resp.content_encoding == "gzip"


def test_content_encoding_none_removes_header():
    resp = Response()
    resp.content_encoding = "br"
    resp.content_encoding = None
    assert "Content-Encoding" not in resp.headers


def test_content_encoding_reads_existing_header():
    resp = Response()
    resp.headers["Content-Encoding"] = "deflate"
    assert resp.content_encoding == "deflate"


# ── content_language ────────────────────────────────────────────────


def test_content_language_none_by_default():
    assert Response().content_language is None


def test_content_language_set_and_read():
    resp = Response()
    resp.content_language = "en-US"
    assert resp.headers["Content-Language"] == "en-US"
    assert resp.content_language == "en-US"


def test_content_language_none_removes_header():
    resp = Response()
    resp.content_language = "fr"
    resp.content_language = None
    assert "Content-Language" not in resp.headers


def test_content_language_multiple_tags():
    resp = Response()
    resp.content_language = "en, fr, de"
    assert resp.content_language == "en, fr, de"
