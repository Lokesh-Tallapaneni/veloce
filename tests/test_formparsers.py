"""Tests for Content-Disposition parsing in veloce.http.formparsers."""

from __future__ import annotations

from veloce.http.formparsers import _parse_content_disposition


def test_quoted_semicolon_preserved_in_name():
    disp, params = _parse_content_disposition('form-data; name="a;b"')
    assert disp == "form-data"
    assert params["name"] == "a;b"


def test_escaped_quote_inside_quoted_value():
    disp, params = _parse_content_disposition(r'form-data; name="a\"b"')
    assert disp == "form-data"
    assert params["name"] == 'a"b'


def test_escaped_backslash_inside_quoted_value():
    disp, params = _parse_content_disposition(r'form-data; name="a\\b"')
    assert disp == "form-data"
    assert params["name"] == "a\\b"


def test_unquoted_value_still_parses():
    disp, params = _parse_content_disposition("form-data; name=plain")
    assert disp == "form-data"
    assert params["name"] == "plain"


def test_whitespace_around_semicolon():
    disp, params = _parse_content_disposition('form-data; name="a" ; filename="report.pdf"')
    assert disp == "form-data"
    assert params["name"] == "a"
    assert params["filename"] == "report.pdf"


def test_parameter_keys_lowercased():
    _, params = _parse_content_disposition('form-data; Name="x"; FileName="y.txt"')
    assert params["name"] == "x"
    assert params["filename"] == "y.txt"


def test_quoted_semicolon_in_filename_with_following_param():
    disp, params = _parse_content_disposition('form-data; name="upload"; filename="weird;name.pdf"')
    assert disp == "form-data"
    assert params["name"] == "upload"
    assert params["filename"] == "weird;name.pdf"
