"""Request.access_route tests."""

from __future__ import annotations

from veloce import Request

# ── Request.access_route ────────────────────────────────────────────


def test_access_route_uses_x_forwarded_for():
    """Chain order: client -> proxies, then peer last."""
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"},
        body=b"",
        scope={"client": ("3.3.3.3", 0)},
    )
    assert req.access_route == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]


def test_access_route_falls_back_to_peer():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={},
        body=b"",
        scope={"client": ("3.3.3.3", 0)},
    )
    assert req.access_route == ["3.3.3.3"]


def test_access_route_empty_when_no_peer():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.access_route == []
