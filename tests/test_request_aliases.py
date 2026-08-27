"""Request.referrer / host / remote_addr / charset — request aliases."""

from __future__ import annotations

from veloce import Request


def _req(headers: dict | None = None, state: dict | None = None) -> Request:
    r = Request(
        method="GET",
        path="/x",
        query_string="",
        headers=headers or {},
        body=b"",
    )
    if state:
        r.state.update(state)
    return r


# ── referrer ─────────────────────────────────────────────────────────


def test_referrer_from_referer_header():
    r = _req({"referer": "https://example.com/from"})
    assert r.referrer == "https://example.com/from"


def test_referrer_empty_when_missing():
    assert _req().referrer == ""


# ── host ─────────────────────────────────────────────────────────────


def test_host_returns_header_value():
    r = _req({"host": "api.example.com"})
    assert r.host == "api.example.com"


def test_host_empty_when_missing():
    assert _req().host == ""


# ── remote_addr ──────────────────────────────────────────────────────


def test_remote_addr_none_when_no_transport_no_proxy_state():
    assert _req().remote_addr is None


def test_remote_addr_honors_proxy_fix_state():
    """When a trusted proxy hop stashed the client IP, that wins."""
    r = _req(state={"proxy_fix_client": "203.0.113.7"})
    assert r.remote_addr == "203.0.113.7"


# ── charset ──────────────────────────────────────────────────────────


def test_charset_default_utf8():
    assert _req({"content-type": "application/json"}).charset == "utf-8"


def test_charset_parsed_from_content_type():
    r = _req({"content-type": "text/html; charset=iso-8859-1"})
    assert r.charset == "iso-8859-1"
