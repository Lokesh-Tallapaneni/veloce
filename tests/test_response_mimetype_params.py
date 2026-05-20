"""Response.mimetype_params — Content-Type parameter accessor."""

from __future__ import annotations

from veloce import Response


def test_empty_when_no_params():
    resp = Response(content_type="text/html")
    assert resp.mimetype_params == {}


def test_charset_param_parsed():
    resp = Response(content_type="text/html; charset=utf-8")
    assert resp.mimetype_params == {"charset": "utf-8"}


def test_multiple_params():
    resp = Response(content_type="multipart/form-data; boundary=abc; charset=ascii")
    assert resp.mimetype_params == {"boundary": "abc", "charset": "ascii"}


def test_quoted_value_unwrapped():
    resp = Response(content_type='application/json; charset="utf-8"')
    assert resp.mimetype_params == {"charset": "utf-8"}


def test_param_name_lowercased():
    resp = Response(content_type="text/plain; Charset=UTF-8")
    assert resp.mimetype_params == {"charset": "UTF-8"}


def test_tracks_charset_setter():
    resp = Response(content_type="text/plain")
    resp.charset = "latin-1"
    assert resp.mimetype_params == {"charset": "latin-1"}


def test_valueless_part_ignored():
    resp = Response(content_type="text/plain; charset=utf-8; flag")
    assert resp.mimetype_params == {"charset": "utf-8"}
