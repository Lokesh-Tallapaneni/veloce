"""Response.accept_ranges / content_range / set_content_range — RFC 9110 §14."""

from __future__ import annotations

from veloce import Response

# ── accept_ranges ───────────────────────────────────────────────────


def test_accept_ranges_none_by_default():
    assert Response().accept_ranges is None


def test_accept_ranges_set_bytes():
    resp = Response()
    resp.accept_ranges = "bytes"
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.accept_ranges == "bytes"


def test_accept_ranges_none_removes_header():
    resp = Response()
    resp.accept_ranges = "bytes"
    resp.accept_ranges = None
    assert "Accept-Ranges" not in resp.headers


# ── set_content_range / content_range ───────────────────────────────


def test_content_range_none_by_default():
    assert Response().content_range is None


def test_set_content_range_full_form():
    resp = Response()
    out = resp.set_content_range(0, 499, 1234)
    assert out == "bytes 0-499/1234"
    assert resp.content_range == "bytes 0-499/1234"


def test_set_content_range_unknown_total():
    resp = Response()
    out = resp.set_content_range(0, 499, None)
    assert out == "bytes 0-499/*"


def test_set_content_range_unsatisfied():
    """No start/stop → `bytes */<length>` (RFC 9110 §14.4)."""
    resp = Response()
    out = resp.set_content_range(None, None, 1234)
    assert out == "bytes */1234"


def test_set_content_range_custom_unit():
    resp = Response()
    out = resp.set_content_range(0, 9, 100, unit="items")
    assert out == "items 0-9/100"


def test_set_content_range_writes_header():
    resp = Response()
    resp.set_content_range(100, 199, 1000)
    assert resp.headers["Content-Range"] == "bytes 100-199/1000"
