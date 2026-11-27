"""`Request.access_route` - the client chain, from `X-Forwarded-For` or the peer.

Named for its subject: the module was `test_header_utils.py` and contained no
header-utility tests at all, so a reader looking for header parsing found route
tests and a reader looking for these could not guess the file.
"""

from __future__ import annotations

from tests.conftest import make_request

# ── Request.access_route ────────────────────────────────────────────


def test_access_route_uses_x_forwarded_for():
    """Chain order: client -> proxies, then peer last."""
    req = make_request(
        method="GET",
        path="/",
        query_string="",
        headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"},
        body=b"",
        scope={"client": ("3.3.3.3", 0)},
    )
    assert req.access_route == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]


def test_access_route_falls_back_to_peer():
    req = make_request(
        method="GET",
        path="/",
        query_string="",
        headers={},
        body=b"",
        scope={"client": ("3.3.3.3", 0)},
    )
    assert req.access_route == ["3.3.3.3"]


def test_access_route_empty_when_no_peer():
    req = make_request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.access_route == []
