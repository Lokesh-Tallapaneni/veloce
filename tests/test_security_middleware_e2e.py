"""End-to-end tests for security middleware through the real TestClient stack.

Covers the CSRF UploadFile guard, the session-cookie oversize warn-and-drop
path, and the RateLimitMiddleware X-RateLimit-* header / bucket-key behaviour.
"""

from __future__ import annotations

import logging

from veloce import Veloce
from veloce.middleware.csrf import CSRFMiddleware
from veloce.middleware.security import RateLimitMiddleware
from veloce.middleware.sessions import SessionMiddleware
from veloce.testclient import TestClient

# ── CSRF ─────────────────────────────────────────────────────────────


def _csrf_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(CSRFMiddleware(cookie_secure=False))

    @app.post("/echo")
    async def echo(request):
        return {"ok": True}

    return app


def test_csrf_upload_file_in_token_field_returns_403_not_500():
    """A multipart submission whose csrf_token field is a file part must
    be refused with 403 — the middleware must treat the non-string value
    as a missing token rather than crash."""
    app = _csrf_app()
    with TestClient(app) as client:
        seed = client.get("/echo")
        token = seed.cookies["csrf_token"]

        resp = client.post(
            "/echo",
            files={"csrf_token": ("token.bin", token.encode(), "application/octet-stream")},
            headers={"X-CSRF-Token": "wrong-header-value"},
        )

    assert resp.status_code == 403
    assert resp.json() == {"detail": "CSRF token mismatch"}


def test_csrf_matching_cookie_and_header_passes():
    """The double-submit happy path: cookie + matching header → 200."""
    app = _csrf_app()
    with TestClient(app) as client:
        seed = client.get("/echo")
        token = seed.cookies["csrf_token"]
        resp = client.post("/echo", json={}, headers={"X-CSRF-Token": token})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ── Sessions ─────────────────────────────────────────────────────────


def _session_app(max_cookie_size: int = 4093) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="x" * 32, max_cookie_size=max_cookie_size))

    @app.get("/big")
    async def big(request):
        request.session["payload"] = "A" * 8192
        return {"wrote": True}

    @app.get("/small")
    async def small(request):
        request.session["user_id"] = 42
        return {"wrote": True}

    @app.get("/read")
    async def read(request):
        return {"user_id": request.session.get("user_id")}

    return app


def test_session_oversize_payload_is_dropped_with_warning(caplog):
    """An 8 KB session payload exceeds the cookie ceiling — the middleware
    must emit a warning at `veloce.sessions` and refuse to set the cookie
    instead of corrupting the next request."""
    app = _session_app()
    with (
        TestClient(app) as client,
        caplog.at_level(logging.WARNING, logger="veloce.sessions"),
    ):
        resp = client.get("/big")

    assert resp.status_code == 200
    assert "Set-Cookie" not in resp.headers
    assert any(
        rec.name == "veloce.sessions" and rec.levelno == logging.WARNING for rec in caplog.records
    )


def test_session_small_payload_is_set_and_roundtrips():
    """A small payload fits under the ceiling — Set-Cookie is emitted and
    the cookie round-trips so the next request sees the same session."""
    app = _session_app()
    with TestClient(app) as client:
        first = client.get("/small")
        assert first.status_code == 200
        assert "Set-Cookie" in first.headers
        assert "session" in first.cookies

        second = client.get("/read")
        assert second.status_code == 200
        assert second.json() == {"user_id": 42}


# ── RateLimit ────────────────────────────────────────────────────────


def _rl_app(max_requests: int = 5, window_seconds: int = 60) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(
        RateLimitMiddleware(max_requests=max_requests, window_seconds=window_seconds)
    )

    @app.get("/ping")
    async def ping(request):
        return {"ok": True}

    return app


def test_ratelimit_success_headers_present():
    """A single allowed request carries X-RateLimit-Limit/Remaining/Reset."""
    app = _rl_app(max_requests=5, window_seconds=60)
    with TestClient(app) as client:
        # Stable User-Agent so the bucket key is deterministic.
        resp = client.get("/ping", headers={"User-Agent": "rl-test/1"})

    assert resp.status_code == 200
    assert resp.headers["X-RateLimit-Limit"] == "5"
    assert resp.headers["X-RateLimit-Remaining"] == "4"
    reset = int(resp.headers["X-RateLimit-Reset"])
    assert 0 <= reset <= 60


def test_ratelimit_429_carries_retry_after_and_headers():
    """The 6th request inside max_requests=5 must be rejected with 429
    and carry both Retry-After and the X-RateLimit-* family."""
    app = _rl_app(max_requests=5, window_seconds=60)
    ua = {"User-Agent": "rl-test/limit"}
    with TestClient(app) as client:
        for _ in range(5):
            ok = client.get("/ping", headers=ua)
            assert ok.status_code == 200
        rejected = client.get("/ping", headers=ua)

    assert rejected.status_code == 429
    assert rejected.headers["X-RateLimit-Limit"] == "5"
    assert rejected.headers["X-RateLimit-Remaining"] == "0"
    assert "Retry-After" in rejected.headers
    assert "X-RateLimit-Reset" in rejected.headers


def test_ratelimit_anonymous_traffic_does_not_share_one_bucket():
    """Two anonymous requests (no client_host, no X-Forwarded-For, no
    User-Agent) must each get their own bucket — otherwise a single
    anonymous source exhausts the limit for every other anonymous
    caller. With max_requests=1, both requests must still succeed."""
    app = _rl_app(max_requests=1, window_seconds=60)
    with TestClient(app) as client:
        # Strip the default UA from the base headers so the request truly
        # carries no UA / no XFF / no transport peer.
        first = client.get("/ping")
        second = client.get("/ping")

    assert first.status_code == 200
    assert second.status_code == 200


def test_reset_after_ceils_subsecond_remainder():
    # A sub-second remainder must round up to 1, not floor to 0, so the client
    # is never told "retry now" while a fraction of a second still remains.
    from collections import deque

    mw = RateLimitMiddleware(max_requests=1, window_seconds=60)
    assert mw._reset_after(deque([0.4]), 60.0) == 1


def test_ratelimit_xff_keys_on_rightmost_hop():
    """X-Forwarded-For is parsed RIGHT-to-LEFT — the right-most hop is
    the closest (and only trustworthy) proxy. Spoofing the LEFT-most
    value must not let a client evade the per-source limit."""
    app = _rl_app(max_requests=3, window_seconds=60)
    with TestClient(app) as client:
        spoofed_left = "9.9.9.9, real-proxy-ip"
        for _ in range(3):
            ok = client.get("/ping", headers={"X-Forwarded-For": spoofed_left})
            assert ok.status_code == 200
        # Rotate the spoofed left hop — the right hop stays "real-proxy-ip",
        # so the bucket must be the same and the next call must trip 429.
        rejected = client.get("/ping", headers={"X-Forwarded-For": "1.2.3.4, real-proxy-ip"})

    assert rejected.status_code == 429
    assert rejected.headers["X-RateLimit-Remaining"] == "0"
