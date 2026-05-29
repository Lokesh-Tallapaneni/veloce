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


# ── S5: signed (HMAC) CSRF token + secure-by-default cookie ───────────


async def test_csrf_cookie_is_secure_by_default():
    """The minted CSRF cookie carries the `Secure` attribute by default."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(token_factory=lambda: "TKN"))

    @app.get("/x")
    async def x():
        return {}

    resp = await app.handle_request(_req("GET"))
    assert "Secure" in resp.headers.get("Set-Cookie", "")


async def test_signed_csrf_rejects_unsigned_injected_token():
    """With a `secret` configured, a cookie value carrying no valid
    server signature is refused even when the header echoes it — the
    cookie-injection scenario the signature closes."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(secret="test-secret"))

    @app.post("/x")
    async def x():
        return {}

    # Attacker plants the same value in cookie and header — but cannot
    # produce a valid signature for it.
    resp = await app.handle_request(
        _req(
            "POST",
            headers={"cookie": "csrf_token=attacker", "x-csrf-token": "attacker"},
        )
    )
    assert resp.status_code == 403


async def test_signed_csrf_roundtrip_passes():
    """A token minted by the signed middleware verifies on the way back."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(secret="test-secret"))

    @app.get("/g")
    async def g():
        return {}

    @app.post("/x")
    async def x():
        return {"ok": True}

    minted = await app.handle_request(_req("GET", path="/g"))
    set_cookie = minted.headers.get("Set-Cookie", "")
    token = set_cookie.split("csrf_token=", 1)[1].split(";", 1)[0]

    resp = await app.handle_request(
        _req(
            "POST",
            headers={"cookie": f"csrf_token={token}", "x-csrf-token": token},
        )
    )
    assert resp.status_code == 200


async def test_signed_csrf_respects_max_age():
    """A signed token older than `max_age` is refused. `max_age=-1`
    rejects even a just-minted token (age >= 0 > -1), proving the bound
    is passed through to signature verification."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(secret="test-secret", max_age=-1))

    @app.get("/g")
    async def g():
        return {}

    @app.post("/x")
    async def x():
        return {}

    minted = await app.handle_request(_req("GET", path="/g"))
    token = minted.headers.get("Set-Cookie", "").split("csrf_token=", 1)[1].split(";", 1)[0]

    resp = await app.handle_request(
        _req("POST", headers={"cookie": f"csrf_token={token}", "x-csrf-token": token})
    )
    assert resp.status_code == 403


# ── Form-field as uploaded file part ─────────────────────────────────


def test_post_with_csrf_form_field_as_uploadfile_is_refused():
    # If the `csrf_token` multipart part arrives as a file upload, `form.get`
    # returns an UploadFile; compare_digest would crash on it. Middleware must
    # treat the non-string value as a missing token and refuse with 403.
    from veloce.testclient import TestClient

    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware())

    @app.post("/x")
    async def x():
        return {}

    with TestClient(app) as client:
        resp = client.post(
            "/x",
            headers={"cookie": "csrf_token=abc123"},
            files={"csrf_token": ("token.txt", b"abc123", "text/plain")},
        )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "CSRF token mismatch"}


@pytest.mark.asyncio
async def test_csrf_header_and_form_paths_use_same_check():
    """Header and form branches must accept and reject the same shapes.

    R1 #11: prior to the `_matches` helper, the header branch used
    `if header_val and ...` while the form branch used
    `isinstance(form_val, str) and ...`. The two were behaviourally
    equivalent for strings but the divergence was a copy-paste hazard;
    pin the equivalence with a single test that runs both branches
    through identical inputs.
    """
    from urllib.parse import urlencode

    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware())

    @app.post("/x")
    async def x():
        return {"ok": True}

    cookie = "csrf_token=tok-XYZ"
    body = urlencode({"csrf_token": "tok-XYZ"}).encode()

    # Form branch accepts the matching string.
    resp = await app.handle_request(
        _req(
            "POST",
            headers={"cookie": cookie, "content-type": "application/x-www-form-urlencoded"},
            body=body,
        )
    )
    assert resp.status_code == 200

    # Header branch accepts the matching string.
    resp = await app.handle_request(
        _req("POST", headers={"cookie": cookie, "x-csrf-token": "tok-XYZ"})
    )
    assert resp.status_code == 200

    # Both branches reject the wrong string identically.
    resp = await app.handle_request(
        _req("POST", headers={"cookie": cookie, "x-csrf-token": "wrong"})
    )
    assert resp.status_code == 403
    resp = await app.handle_request(
        _req(
            "POST",
            headers={"cookie": cookie, "content-type": "application/x-www-form-urlencoded"},
            body=urlencode({"csrf_token": "wrong"}).encode(),
        )
    )
    assert resp.status_code == 403
