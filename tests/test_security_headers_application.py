"""Hardening headers are applied with one pass over the response's keys.

The middleware asked `header_present` once per default header. That helper
returns fast when the header is stored under the exact key it was given, and
otherwise scans every response header — and the common case is that the handler
set none of these, so every one of the three to six calls took the full scan.

The behaviour is what matters and none of it changes: a default is added only
when the handler did not set that header, matched case-insensitively, because
`Response.headers` is a plain dict and a handler-set `x-frame-options` must
count as an override of the `X-Frame-Options` default.
"""

from __future__ import annotations

import pytest

from veloce import Request, Response, SecurityHeadersMiddleware, Veloce
from veloce.http.response import header_present
from veloce.testclient import TestClient


def _request() -> Request:
    """A fresh request per call.

    One module-level instance shared by every test is state a test can leave a
    mark on - `Request` carries `state` and cached-property slots - and the
    next failure would look like the middleware's.
    """
    return Request(method="GET", path="/", query_string="", headers={}, body=b"")


async def _applied(middleware: SecurityHeadersMiddleware, headers: dict | None = None) -> dict:
    response = Response(body=b"x", headers=dict(headers or {}))
    return (await middleware.process_response(_request(), response)).headers


# ── the defaults ─────────────────────────────────────────────────────


async def test_the_three_always_on_headers_are_added():
    headers = await _applied(SecurityHeadersMiddleware())
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


async def test_the_opt_in_headers_are_added_when_configured():
    headers = await _applied(
        SecurityHeadersMiddleware(
            hsts_max_age=31536000,
            content_security_policy="default-src 'self'",
            permissions_policy="geolocation=()",
        )
    )
    assert headers["Strict-Transport-Security"] == "max-age=31536000"
    assert headers["Content-Security-Policy"] == "default-src 'self'"
    assert headers["Permissions-Policy"] == "geolocation=()"


async def test_an_opt_in_header_is_absent_when_not_configured():
    headers = await _applied(SecurityHeadersMiddleware())
    assert "Strict-Transport-Security" not in headers
    assert "Content-Security-Policy" not in headers


async def test_a_response_with_no_headers_gets_all_the_defaults():
    """The empty case takes a separate branch; it must produce the same result."""
    headers = await _applied(SecurityHeadersMiddleware(hsts_max_age=1))
    assert sorted(headers) == [
        "Referrer-Policy",
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "X-Frame-Options",
    ]


# ── the handler always wins ──────────────────────────────────────────


async def test_a_handler_value_is_not_overwritten():
    headers = await _applied(SecurityHeadersMiddleware(), {"X-Frame-Options": "SAMEORIGIN"})
    assert headers["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.parametrize(
    "spelling", ["x-frame-options", "X-FRAME-OPTIONS", "X-Frame-Options", "x-FrAmE-oPtIoNs"]
)
async def test_a_handler_value_wins_under_any_casing(spelling):
    """The reason the old code scanned: a plain dict has no case-insensitivity."""
    headers = await _applied(SecurityHeadersMiddleware(), {spelling: "SAMEORIGIN"})
    assert headers[spelling] == "SAMEORIGIN"
    assert "X-Frame-Options" not in headers or headers.get("X-Frame-Options") == "SAMEORIGIN"


async def test_overriding_one_header_does_not_suppress_the_others():
    headers = await _applied(SecurityHeadersMiddleware(), {"x-frame-options": "SAMEORIGIN"})
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


async def test_a_handler_can_override_every_default():
    middleware = SecurityHeadersMiddleware(hsts_max_age=1)
    supplied = {
        "x-content-type-options": "mine",
        "x-frame-options": "mine",
        "referrer-policy": "mine",
        "strict-transport-security": "mine",
    }
    headers = await _applied(middleware, supplied)
    assert all(value == "mine" for key, value in headers.items() if key in supplied)
    assert len(headers) == len(supplied)


async def test_unrelated_handler_headers_are_left_alone():
    headers = await _applied(SecurityHeadersMiddleware(), {"X-Request-Id": "abc"})
    assert headers["X-Request-Id"] == "abc"
    assert headers["X-Content-Type-Options"] == "nosniff"


# ── the precomputed table matches what is sent ───────────────────────


def test_the_comparison_keys_match_the_configured_headers():
    """The lowered forms are settled at construction; they must not drift."""
    middleware = SecurityHeadersMiddleware(hsts_max_age=1, content_security_policy="x")
    assert [name for name, _lowered, _value in middleware._header_items] == list(
        middleware._headers
    )
    for name, lowered, value in middleware._header_items:
        assert lowered == name.lower()
        assert middleware._headers[name] == value


def test_two_middlewares_with_different_options_keep_their_own_tables():
    plain = SecurityHeadersMiddleware()
    hardened = SecurityHeadersMiddleware(hsts_max_age=1)
    assert len(plain._header_items) == 3
    assert len(hardened._header_items) == 4


# ── end to end ───────────────────────────────────────────────────────


def test_the_headers_reach_a_real_response():
    app = Veloce(openapi_url=None)
    app.add_middleware(SecurityHeadersMiddleware(hsts_max_age=31536000))

    @app.get("/x")
    async def x() -> dict:
        return {}

    response = TestClient(app).get("/x")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_a_handler_set_header_survives_to_the_wire():
    app = Veloce(openapi_url=None)
    app.add_middleware(SecurityHeadersMiddleware())

    @app.get("/x")
    async def x():
        return Response(
            body=b"{}", content_type="application/json", headers={"X-Frame-Options": "SAMEORIGIN"}
        )

    assert TestClient(app).get("/x").headers["X-Frame-Options"] == "SAMEORIGIN"


def test_an_error_response_also_carries_the_headers():
    app = Veloce(openapi_url=None)
    app.add_middleware(SecurityHeadersMiddleware())

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert TestClient(app).get("/missing").headers["X-Content-Type-Options"] == "nosniff"


# ── the output is identical to what the scan produced ────────────────


@pytest.mark.parametrize("hsts", [None, 31536000])
@pytest.mark.parametrize("csp", [None, "default-src 'self'"])
@pytest.mark.parametrize(
    "supplied",
    [
        {},
        {"X-Frame-Options": "SAMEORIGIN"},
        {"x-frame-options": "SAMEORIGIN"},
        {"X-Request-Id": "abc"},
        {"x-content-type-options": "mine", "REFERRER-POLICY": "mine"},
        {f"X-H{i}": "v" for i in range(8)},
    ],
)
async def test_the_result_matches_a_header_by_header_scan(hsts, csp, supplied):
    """A perf change must produce byte-identical output.

    This recomputes the answer the way the replaced code did - one
    case-insensitive lookup per default - and requires the two to agree across
    every combination of configured defaults and handler-set headers.
    """

    middleware = SecurityHeadersMiddleware(hsts_max_age=hsts, content_security_policy=csp)

    expected = dict(supplied)
    for name, value in middleware._headers.items():
        if not header_present(expected, name):
            expected[name] = value

    assert await _applied(middleware, supplied) == expected
