"""CSRFMiddleware — double-submit-cookie."""

from __future__ import annotations

import pytest

from veloce import CSRFMiddleware, Request, Veloce


def _req(method: str, path: str = "/x", headers: dict | None = None, body: bytes = b"") -> Request:
    return Request(
        method=method,
        path=path,
        query_string="",
        headers=headers or {},
        body=body,
    )


# ── Safe methods bypass the check ────────────────────────────────────


@pytest.mark.asyncio
async def test_safe_method_no_cookie_passes_and_mints_cookie():
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(token_factory=lambda: "DETERMINISTIC"))

    @app.get("/x")
    async def x():
        return {}

    resp = await app.handle_request(_req("GET"))
    assert resp.status_code == 200
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "csrf_token=DETERMINISTIC" in set_cookie


# ── State-changing methods require matching token ────────────────────


@pytest.mark.asyncio
async def test_post_without_cookie_is_refused():
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware())

    @app.post("/x")
    async def x():
        return {}

    resp = await app.handle_request(_req("POST"))
    assert resp.status_code == 403
    import orjson

    assert orjson.loads(resp.body) == {"detail": "CSRF cookie missing"}


@pytest.mark.asyncio
async def test_post_with_matching_header_passes():
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware())

    @app.post("/x")
    async def x():
        return {"ok": True}

    resp = await app.handle_request(
        _req(
            "POST",
            headers={
                "cookie": "csrf_token=abc123",
                "x-csrf-token": "abc123",
            },
        )
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_with_mismatched_header_refused():
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware())

    @app.post("/x")
    async def x():
        return {}

    resp = await app.handle_request(
        _req(
            "POST",
            headers={
                "cookie": "csrf_token=abc123",
                "x-csrf-token": "WRONG",
            },
        )
    )
    assert resp.status_code == 403


# ── Form-field fallback ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_with_matching_form_field_passes():
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware())

    @app.post("/x")
    async def x():
        return {}

    resp = await app.handle_request(
        _req(
            "POST",
            headers={
                "cookie": "csrf_token=abc123",
                "content-type": "application/x-www-form-urlencoded",
            },
            body=b"csrf_token=abc123&other=stuff",
        )
    )
    assert resp.status_code == 200


# ── Cookie-mint idempotency ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_existing_cookie_not_overwritten():
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(token_factory=lambda: "NEW"))

    @app.get("/x")
    async def x():
        return {}

    resp = await app.handle_request(_req("GET", headers={"cookie": "csrf_token=EXISTING"}))
    # Cookie already set; middleware doesn't replace it.
    assert "Set-Cookie" not in resp.headers


# ── Custom header / form-field names ─────────────────────────────────


@pytest.mark.asyncio
async def test_custom_header_name_honored():
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(header_name="X-XSRF-TOKEN"))

    @app.post("/x")
    async def x():
        return {}

    resp = await app.handle_request(
        _req(
            "POST",
            headers={
                "cookie": "csrf_token=t",
                "x-xsrf-token": "t",
            },
        )
    )
    assert resp.status_code == 200
