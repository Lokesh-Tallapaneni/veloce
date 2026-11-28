"""Response.retry_after / Response.age — typed header accessors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from veloce import Response

# ── retry_after ─────────────────────────────────────────────────────


def test_retry_after_none_by_default():
    assert Response().retry_after is None


def test_retry_after_set_int_seconds():
    resp = Response()
    resp.retry_after = 120
    assert resp.headers["Retry-After"] == "120"
    assert resp.retry_after == 120


def test_retry_after_set_timedelta():
    resp = Response()
    resp.retry_after = timedelta(minutes=5)
    assert resp.headers["Retry-After"] == "300"
    assert resp.retry_after == 300


def test_retry_after_set_datetime():
    resp = Response()
    dt = datetime(1994, 11, 6, 8, 49, 37, tzinfo=timezone.utc)
    resp.retry_after = dt
    assert resp.headers["Retry-After"] == "Sun, 06 Nov 1994 08:49:37 GMT"
    # Reads back as a datetime.
    assert resp.retry_after == dt


def test_retry_after_none_removes_header():
    resp = Response()
    resp.retry_after = 60
    resp.retry_after = None
    assert "Retry-After" not in resp.headers
    assert resp.retry_after is None


# ── age ─────────────────────────────────────────────────────────────


def test_age_none_by_default():
    assert Response().age is None


def test_age_set_and_read():
    resp = Response()
    resp.age = 42
    assert resp.headers["Age"] == "42"
    assert resp.age == 42


def test_age_none_removes_header():
    resp = Response()
    resp.age = 10
    resp.age = None
    assert "Age" not in resp.headers
    assert resp.age is None


def test_age_non_numeric_header_returns_none():
    resp = Response()
    resp.headers["Age"] = "not-a-number"
    assert resp.age is None
