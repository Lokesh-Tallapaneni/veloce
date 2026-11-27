"""`Response.charset` — the accessor and the setter.

The single home for charset behaviour. `test_response_shape_aliases.py` also
carried a default / parameter / quoted-value trio, each a subset of the
parametrized cases below; they are gone rather than kept in parallel, and that
module is about `content_length`, `is_streamed` and the `environ` alias.

The neighbouring `test_response_mimetype.py` and
`test_response_mimetype_params.py` are separate on purpose: each is named for
one accessor of the same header, and `test_charset_agrees_with_mimetype_params`
below is what ties them together.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_request
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


# ── charset accessor: RFC 9110 Sec. 8.3.1 parameter-name case-insensitivity ──


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html; charset=iso-8859-1",
        "text/html; Charset=iso-8859-1",
        "text/html; CHARSET=iso-8859-1",
        "text/html; charset = iso-8859-1",
        'text/html; charset="iso-8859-1"',
        "text/html;charset=iso-8859-1",
        "text/html; boundary=x; charset=iso-8859-1",
    ],
)
def test_charset_reads_every_spelling(content_type):
    assert Response(content_type=content_type).charset == "iso-8859-1"


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "text/html; charset=iso-8859-1",
        "text/html; Charset=iso-8859-1",
        "text/html; CHARSET=iso-8859-1",
        "text/html; charset = iso-8859-1",
        'text/html; charset="iso-8859-1"',
        "text/html; boundary=x; charset=iso-8859-1",
        "text/plain; charset=utf-8; flag",
    ],
)
def test_charset_agrees_with_mimetype_params(content_type):
    resp = Response(content_type=content_type)
    assert resp.charset == resp.mimetype_params.get("charset", "utf-8")


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "text/html; charset=iso-8859-1",
        "text/html; Charset=iso-8859-1",
        "text/html; CHARSET=iso-8859-1",
        "text/html; charset = iso-8859-1",
        'text/html; charset="iso-8859-1"',
        "text/html; boundary=x; charset=iso-8859-1",
    ],
)
def test_charset_agrees_with_request_charset(content_type):
    """The two doors read one header the same way."""
    req = make_request(
        method="GET", path="/x", query_string="", headers={"content-type": content_type}, body=b""
    )
    assert Response(content_type=content_type).charset == req.charset


def test_charset_value_case_preserved():
    """Only the parameter NAME is case-insensitive; the value is verbatim."""
    assert Response(content_type="text/plain; Charset=UTF-8").charset == "UTF-8"


def test_charset_ignores_other_parameters():
    resp = Response(content_type="multipart/form-data; boundary=abc")
    assert resp.charset == "utf-8"


def test_mimetype_setter_preserves_mixed_case_charset():
    resp = Response(content_type="text/plain; Charset=iso-8859-1")
    resp.mimetype = "text/html"
    assert resp.mimetype == "text/html"
    assert resp.charset == "iso-8859-1"
