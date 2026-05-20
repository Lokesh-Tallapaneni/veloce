"""Response.date / location / content_location + Request.date accessors."""

from __future__ import annotations

from datetime import datetime, timezone

from veloce import Request, Response

# ── Response.date ───────────────────────────────────────────────────


def test_response_date_none_by_default():
    assert Response().date is None


def test_response_date_set_datetime():
    resp = Response()
    dt = datetime(1994, 11, 6, 8, 49, 37, tzinfo=timezone.utc)
    resp.date = dt
    assert resp.headers["Date"] == "Sun, 06 Nov 1994 08:49:37 GMT"
    assert resp.date == dt


def test_response_date_set_timestamp():
    resp = Response()
    resp.date = 784111777
    assert resp.headers["Date"] == "Sun, 06 Nov 1994 08:49:37 GMT"


def test_response_date_none_removes_header():
    resp = Response()
    resp.date = 0
    resp.date = None
    assert "Date" not in resp.headers


# ── Response.location ───────────────────────────────────────────────


def test_location_none_by_default():
    assert Response().location is None


def test_location_set_and_read():
    resp = Response()
    resp.location = "/new-path"
    assert resp.headers["Location"] == "/new-path"
    assert resp.location == "/new-path"


def test_location_none_removes_header():
    resp = Response()
    resp.location = "/x"
    resp.location = None
    assert "Location" not in resp.headers


# ── Response.content_location ───────────────────────────────────────


def test_content_location_none_by_default():
    assert Response().content_location is None


def test_content_location_set_and_read():
    resp = Response()
    resp.content_location = "/canonical/resource"
    assert resp.headers["Content-Location"] == "/canonical/resource"
    assert resp.content_location == "/canonical/resource"


# ── Request.date ────────────────────────────────────────────────────


def test_request_date_parses_header():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"Date": "Sun, 06 Nov 1994 08:49:37 GMT"},
        body=b"",
    )
    assert req.date == datetime(1994, 11, 6, 8, 49, 37, tzinfo=timezone.utc)


def test_request_date_none_when_absent():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.date is None


def test_request_date_none_when_garbage():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"Date": "not a date"},
        body=b"",
    )
    assert req.date is None
