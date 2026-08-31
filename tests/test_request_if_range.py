"""Request.if_range — RFC 9110 §13.1.5."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Request


def _req(headers: dict) -> Request:
    return make_request(method="GET", path="/x", query_string="", headers=headers, body=b"")


def test_missing_returns_empty():
    assert _req({}).if_range == ("", None)


def test_etag_form_strong():
    assert _req({"if-range": '"v1"'}).if_range == ('"v1"', None)


def test_etag_form_weak():
    """W/"..." weak prefix is preserved verbatim."""
    assert _req({"if-range": 'W/"v1"'}).if_range == ('W/"v1"', None)


def test_date_form():
    r = _req({"if-range": "Tue, 01 Jan 2030 00:00:00 GMT"})
    etag, ts = r.if_range
    assert etag == ""
    assert ts == 1893456000.0


def test_malformed_returns_empty():
    assert _req({"if-range": "not-a-date"}).if_range == ("", None)
