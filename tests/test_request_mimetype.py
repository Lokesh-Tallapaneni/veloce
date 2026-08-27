"""Request.mimetype + mimetype_params tests (Q17)."""

from __future__ import annotations

from veloce import Request


def _req(content_type: str | None = None) -> Request:
    headers = {"content-type": content_type} if content_type else {}
    return Request(method="POST", path="/", query_string="", headers=headers, body=b"")


# ── mimetype ──────────────────────────────────────────────────────────


def test_mimetype_without_params():
    assert _req("application/json").mimetype == "application/json"


def test_mimetype_strips_params():
    assert _req("application/json; charset=utf-8").mimetype == "application/json"


def test_mimetype_lowercased():
    """RFC 9110 §8.3: media type is case-insensitive."""
    assert _req("APPLICATION/JSON").mimetype == "application/json"


def test_mimetype_strips_whitespace():
    assert _req("  text/html  ").mimetype == "text/html"


def test_mimetype_no_header_returns_empty():
    assert _req().mimetype == ""


# ── mimetype_params ───────────────────────────────────────────────────


def test_mimetype_params_charset():
    assert _req("text/html; charset=utf-8").mimetype_params == {"charset": "utf-8"}


def test_mimetype_params_multiple():
    p = _req("multipart/form-data; boundary=abc; charset=utf-8").mimetype_params
    assert p == {"boundary": "abc", "charset": "utf-8"}


def test_mimetype_params_quoted_value():
    """Quoted values have their surrounding double-quotes stripped."""
    p = _req('text/plain; charset="utf-8"').mimetype_params
    assert p == {"charset": "utf-8"}


def test_mimetype_params_keys_lowercased():
    """Param keys are case-insensitive per RFC 9110; lowercase them."""
    p = _req("text/html; Charset=utf-8; Boundary=X").mimetype_params
    assert p == {"charset": "utf-8", "boundary": "X"}


def test_mimetype_params_value_case_preserved():
    """Value casing is meaningful for some params (e.g. boundary strings)."""
    p = _req("text/plain; charset=UTF-8").mimetype_params
    assert p["charset"] == "UTF-8"


def test_mimetype_params_no_params_returns_empty_dict():
    assert _req("application/json").mimetype_params == {}


def test_mimetype_params_no_header_returns_empty_dict():
    assert _req().mimetype_params == {}


def test_mimetype_params_malformed_segment_skipped():
    """A param without `=` is skipped, not raised."""
    p = _req("text/html; charset=utf-8; nokey").mimetype_params
    assert p == {"charset": "utf-8"}


# ── is_json includes the +json structured suffix ────────────────
#
# Moved here from `test_formdata_multidict.py`, which covered three unrelated
# subsystems behind opaque tracker tags.


def test_is_json_for_plain_application_json():
    req = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/json"},
        body=b"{}",
    )
    assert req.is_json is True


def test_is_json_for_structured_suffix():
    """RFC 6839 §3.1: `application/vnd.api+json` is JSON-encoded."""
    req = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/vnd.api+json"},
        body=b"{}",
    )
    assert req.is_json is True

    req2 = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/problem+json"},
        body=b"{}",
    )
    assert req2.is_json is True


def test_is_json_strips_charset_param():
    req = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/json; charset=utf-8"},
        body=b"{}",
    )
    assert req.is_json is True


def test_is_json_false_for_form_payloads():
    req = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/x-www-form-urlencoded"},
        body=b"a=1",
    )
    assert req.is_json is False


def test_is_json_false_for_text_plain():
    req = Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "text/plain"},
        body=b"hi",
    )
    assert req.is_json is False


def test_is_json_false_for_missing_content_type():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.is_json is False
