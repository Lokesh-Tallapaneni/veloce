"""Request.is_multipart / is_form / content_encoding accessors."""

from __future__ import annotations

from veloce import Request


def _req(ct: str = "", encoding: str = "") -> Request:
    headers = {}
    if ct:
        headers["content-type"] = ct
    if encoding:
        headers["content-encoding"] = encoding
    return Request(method="POST", path="/x", query_string="", headers=headers, body=b"")


# ── is_multipart ─────────────────────────────────────────────────────


def test_is_multipart_form_data():
    assert _req("multipart/form-data; boundary=----X").is_multipart is True


def test_is_multipart_mixed():
    assert _req("multipart/mixed").is_multipart is True


def test_is_multipart_false_for_urlencoded():
    assert _req("application/x-www-form-urlencoded").is_multipart is False


def test_is_multipart_false_for_no_body():
    assert _req().is_multipart is False


# ── is_form ──────────────────────────────────────────────────────────


def test_is_form_urlencoded_true():
    assert _req("application/x-www-form-urlencoded").is_form is True


def test_is_form_multipart_true():
    assert _req("multipart/form-data; boundary=X").is_form is True


def test_is_form_json_false():
    assert _req("application/json").is_form is False


# ── content_encoding ─────────────────────────────────────────────────


def test_content_encoding_missing_returns_empty():
    assert _req().content_encoding == ""


def test_content_encoding_lowercased():
    assert _req(encoding="GZIP").content_encoding == "gzip"


def test_content_encoding_stripped():
    assert _req(encoding="  br  ").content_encoding == "br"
