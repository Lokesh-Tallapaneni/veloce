"""Response.vary / Response.allow — HeaderSet-backed accessors."""

from __future__ import annotations

from veloce import Response
from veloce.http.header_set import HeaderSet

# ── Response.vary ───────────────────────────────────────────────────


def test_vary_empty_by_default():
    resp = Response()
    assert isinstance(resp.vary, HeaderSet)
    assert len(resp.vary) == 0


def test_vary_reads_existing_header():
    resp = Response()
    resp.headers["Vary"] = "Accept-Encoding, Accept-Language"
    assert "accept-encoding" in resp.vary
    assert "Accept-Language" in resp.vary


def test_vary_setter_from_list():
    resp = Response()
    resp.vary = ["Accept", "Cookie"]
    assert resp.headers["Vary"] == "Accept, Cookie"


def test_vary_setter_from_string():
    resp = Response()
    resp.vary = "Accept-Encoding"
    assert "accept-encoding" in resp.vary


def test_vary_setter_from_headerset():
    resp = Response()
    hs = HeaderSet(["A", "B"])
    resp.vary = hs
    assert resp.headers["Vary"] == "A, B"


def test_add_vary_still_works_alongside_property():
    resp = Response()
    resp.add_vary("Accept")
    assert "accept" in resp.vary


# ── Response.allow ──────────────────────────────────────────────────


def test_allow_empty_by_default():
    resp = Response()
    assert len(resp.allow) == 0


def test_allow_reads_existing_header():
    resp = Response()
    resp.headers["Allow"] = "GET, POST, DELETE"
    allow = resp.allow
    assert "GET" in allow
    assert "post" in allow  # case-insensitive
    assert "PUT" not in allow


def test_allow_setter_from_list():
    resp = Response()
    resp.allow = ["GET", "HEAD", "OPTIONS"]
    assert resp.headers["Allow"] == "GET, HEAD, OPTIONS"


def test_allow_setter_dedupes_case_insensitively():
    resp = Response()
    resp.allow = ["GET", "get", "POST"]
    assert resp.headers["Allow"] == "GET, POST"


def test_allow_roundtrip():
    resp = Response()
    resp.allow = "GET, POST"
    again = resp.allow
    assert set(again) == {"GET", "POST"}
