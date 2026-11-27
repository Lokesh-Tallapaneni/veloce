"""Request scheme / is_secure derived from the ASGI scope."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Request


def _req(headers: dict | None = None, scope_scheme: str | None = None) -> Request:
    scope = {"scheme": scope_scheme} if scope_scheme is not None else None
    return make_request(
        method="GET",
        path="/",
        query_string="",
        headers=headers or {},
        body=b"",
        scope=scope,
    )


# ── scope.scheme is authoritative ─────────────────────────────────────


def test_https_from_scope_scheme():
    r = _req(scope_scheme="https")
    assert r.scheme == "https"
    assert r.is_secure is True


def test_http_from_scope_scheme():
    r = _req(scope_scheme="http")
    assert r.scheme == "http"
    assert r.is_secure is False


# ── X-Forwarded-Proto fallback only when scope.scheme missing ────────


def test_x_forwarded_proto_used_when_no_scope():
    r = _req(headers={"x-forwarded-proto": "https"})
    assert r.scheme == "https"
    assert r.is_secure is True


def test_scope_scheme_wins_over_x_forwarded_proto():
    """Trusted ASGI scheme beats untrusted forwarded header."""
    r = _req(headers={"x-forwarded-proto": "https"}, scope_scheme="http")
    assert r.scheme == "http"


# ── Default ──────────────────────────────────────────────────────────


def test_default_is_http():
    r = _req()
    assert r.scheme == "http"
    assert r.is_secure is False
