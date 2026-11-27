"""Response.last_modified + Response.expires parsed-datetime properties."""

from __future__ import annotations

import datetime as dt

from veloce import Response

# ── last_modified ────────────────────────────────────────────────────


def test_last_modified_returns_none_when_unset():
    assert Response().last_modified is None


def test_last_modified_parses_imf_fixdate():
    resp = Response(headers={"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"})
    parsed = resp.last_modified
    assert parsed == dt.datetime(2024, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)


def test_last_modified_setter_from_datetime():
    resp = Response()
    resp.last_modified = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
    assert "01 Jan 2030" in resp.headers["Last-Modified"]


def test_last_modified_setter_naive_treated_as_utc():
    resp = Response()
    resp.last_modified = dt.datetime(2030, 1, 1)
    assert "01 Jan 2030" in resp.headers["Last-Modified"]


def test_last_modified_setter_from_unix_timestamp():
    resp = Response()
    resp.last_modified = 1893456000  # 2030-01-01 UTC
    assert "01 Jan 2030" in resp.headers["Last-Modified"]


def test_last_modified_setter_none_removes_header():
    resp = Response(headers={"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"})
    resp.last_modified = None
    assert "Last-Modified" not in resp.headers


def test_last_modified_unparseable_returns_none():
    resp = Response(headers={"Last-Modified": "not-a-date"})
    assert resp.last_modified is None


# ── expires ──────────────────────────────────────────────────────────


def test_expires_round_trip():
    resp = Response()
    resp.expires = dt.datetime(2030, 6, 15, tzinfo=dt.timezone.utc)
    parsed = resp.expires
    assert parsed == dt.datetime(2030, 6, 15, tzinfo=dt.timezone.utc)


def test_expires_none_removes_header():
    resp = Response(headers={"Expires": "Mon, 01 Jan 2024 00:00:00 GMT"})
    resp.expires = None
    assert "Expires" not in resp.headers
