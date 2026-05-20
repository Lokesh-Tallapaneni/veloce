"""Response.cookies + Response.headerlist accessors."""

from __future__ import annotations

from veloce import Response

# ── cookies ──────────────────────────────────────────────────────────


def test_no_set_cookie_returns_empty_dict():
    assert Response().cookies == {}


def test_single_set_cookie_parsed():
    resp = Response()
    resp.set_cookie("session", "abc123")
    assert resp.cookies == {"session": "abc123"}


def test_multiple_set_cookies_parsed():
    resp = Response()
    resp.set_cookie("a", "1")
    resp.set_cookie("b", "2")
    assert resp.cookies == {"a": "1", "b": "2"}


def test_duplicate_name_keeps_last():
    """Wire behaviour: client also keeps the most-recent value."""
    resp = Response()
    resp.set_cookie("session", "old")
    resp.set_cookie("session", "new")
    assert resp.cookies == {"session": "new"}


def test_cookies_only_takes_name_value_segment():
    """Path/Secure/HttpOnly etc. shouldn't pollute the value."""
    resp = Response()
    resp.set_cookie("k", "v", path="/", secure=True, httponly=True)
    assert resp.cookies == {"k": "v"}


# ── headerlist ───────────────────────────────────────────────────────


def test_headerlist_flattens_simple_headers():
    resp = Response(headers={"X-A": "1", "X-B": "2"})
    rows = resp.headerlist
    pairs = {(k, v) for k, v in rows}
    assert ("X-A", "1") in pairs
    assert ("X-B", "2") in pairs


def test_headerlist_expands_multi_set_cookie():
    resp = Response()
    resp.set_cookie("a", "1")
    resp.set_cookie("b", "2")
    set_cookies = [(k, v) for k, v in resp.headerlist if k == "Set-Cookie"]
    assert len(set_cookies) == 2
    values = {v.split(";", 1)[0] for _, v in set_cookies}
    assert values == {"a=1", "b=2"}


def test_headerlist_preserves_other_headers_alongside_set_cookie():
    resp = Response()
    resp.set_cookie("k", "v")
    resp.headers["X-Custom"] = "yes"
    rows = resp.headerlist
    assert ("X-Custom", "yes") in [(k, v) for k, v in rows]
