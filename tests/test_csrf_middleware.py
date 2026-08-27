"""CSRFMiddleware — double-submit-cookie."""

from __future__ import annotations

import orjson

from tests.conftest import make_request
from veloce import CSRFMiddleware, Request, Veloce
from veloce.testclient import TestClient


def _req(method: str, path: str = "/x", headers: dict | None = None, body: bytes = b"") -> Request:
    return make_request(
        method=method,
        path=path,
        query_string="",
        headers=headers or {},
        body=body,
    )


# ── Safe methods bypass the check ────────────────────────────────────


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


async def test_query_is_safe_and_bypasses_csrf():
    # QUERY is safe (RFC 10008), so the default safe-method set exempts it from
    # the token check the same way GET is exempted.
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware())

    @app.query("/x")
    async def x():
        return {}

    resp = await app.handle_request(_req("QUERY"))
    assert resp.status_code == 200


# ── State-changing methods require matching token ────────────────────


async def test_post_without_cookie_is_refused():
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware())

    @app.post("/x")
    async def x():
        return {}

    resp = await app.handle_request(_req("POST"))
    assert resp.status_code == 403
    assert orjson.loads(resp.body) == {"detail": "CSRF cookie missing"}


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


# ── Origin-first verification stage ──────────────────────────────────


async def test_origin_first_matching_origin_passes():
    """With trusted_origins set, a matching Origin plus valid double-submit passes."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(trusted_origins=("https://app.example.com",)))

    @app.post("/x")
    async def x():
        return {"ok": True}

    resp = await app.handle_request(
        _req(
            "POST",
            headers={
                "host": "app.example.com",
                "origin": "https://app.example.com",
                "cookie": "csrf_token=tok",
                "x-csrf-token": "tok",
            },
        )
    )
    assert resp.status_code == 200


async def test_origin_first_foreign_origin_refused_before_token():
    """A foreign Origin is a hard 403 even when double-submit would pass."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(trusted_origins=("https://app.example.com",)))

    @app.post("/x")
    async def x():
        return {}

    resp = await app.handle_request(
        _req(
            "POST",
            headers={
                "host": "app.example.com",
                "origin": "https://evil.example.org",
                # Attacker who injected a matching cookie+header still loses.
                "cookie": "csrf_token=tok",
                "x-csrf-token": "tok",
            },
        )
    )
    assert resp.status_code == 403
    assert orjson.loads(resp.body) == {"detail": "CSRF origin mismatch"}


async def test_origin_first_own_origin_always_trusted():
    """The request's own scheme://host is trusted without listing it."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(trusted_origins=("https://other.example.com",)))

    @app.post("/x")
    async def x():
        return {"ok": True}

    req = Request(
        method="POST",
        path="/x",
        query_string="",
        headers={
            "host": "self.example.com",
            "origin": "https://self.example.com",
            "cookie": "csrf_token=tok",
            "x-csrf-token": "tok",
        },
        body=b"",
        scope={"type": "http", "scheme": "https"},
    )
    resp = await app.handle_request(req)
    assert resp.status_code == 200


async def test_origin_first_subdomain_wildcard():
    """A leading-dot trusted host matches the host and its subdomains."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(trusted_origins=("https://.example.com",)))

    @app.post("/x")
    async def x():
        return {"ok": True}

    resp = await app.handle_request(
        _req(
            "POST",
            headers={
                "host": "api.internal",
                "origin": "https://team.example.com",
                "cookie": "csrf_token=tok",
                "x-csrf-token": "tok",
            },
        )
    )
    assert resp.status_code == 200

    # A look-alike suffix attack (exampleXcom) must not match.
    resp = await app.handle_request(
        _req(
            "POST",
            headers={
                "host": "api.internal",
                "origin": "https://evilexample.com",
                "cookie": "csrf_token=tok",
                "x-csrf-token": "tok",
            },
        )
    )
    assert resp.status_code == 403


async def test_origin_first_https_missing_origin_requires_referer():
    """On https with no Origin, a missing Referer is rejected; a matching one passes."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(trusted_origins=("https://app.example.com",)))

    @app.post("/x")
    async def x():
        return {"ok": True}

    # https scope, no Origin, no Referer -> refused.
    scope = {"type": "http", "scheme": "https"}
    req = Request(
        method="POST",
        path="/x",
        query_string="",
        headers={
            "host": "app.example.com",
            "cookie": "csrf_token=tok",
            "x-csrf-token": "tok",
        },
        body=b"",
        scope=scope,
    )
    resp = await app.handle_request(req)
    assert resp.status_code == 403
    assert orjson.loads(resp.body) == {"detail": "CSRF referer missing"}

    # https scope, no Origin, matching Referer -> passes.
    req = Request(
        method="POST",
        path="/x",
        query_string="",
        headers={
            "host": "app.example.com",
            "referer": "https://app.example.com/some/page",
            "cookie": "csrf_token=tok",
            "x-csrf-token": "tok",
        },
        body=b"",
        scope=scope,
    )
    resp = await app.handle_request(req)
    assert resp.status_code == 200


async def test_origin_first_http_no_origin_falls_through_to_double_submit():
    """Plain-http API client with no Origin defers to the double-submit factor."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(trusted_origins=("https://app.example.com",)))

    @app.post("/x")
    async def x():
        return {"ok": True}

    # No scheme in scope -> defaults to http. No Origin. Double-submit decides.
    resp = await app.handle_request(
        _req(
            "POST",
            headers={
                "host": "app.example.com",
                "cookie": "csrf_token=tok",
                "x-csrf-token": "tok",
            },
        )
    )
    assert resp.status_code == 200

    # Mismatched double-submit still loses.
    resp = await app.handle_request(
        _req(
            "POST",
            headers={
                "host": "app.example.com",
                "cookie": "csrf_token=tok",
                "x-csrf-token": "WRONG",
            },
        )
    )
    assert resp.status_code == 403


async def test_origin_first_double_submit_still_enforced():
    """A trusted Origin does not waive the double-submit second factor."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(CSRFMiddleware(trusted_origins=("https://app.example.com",)))

    @app.post("/x")
    async def x():
        return {}

    resp = await app.handle_request(
        _req(
            "POST",
            headers={
                "host": "app.example.com",
                "origin": "https://app.example.com",
                "cookie": "csrf_token=tok",
                "x-csrf-token": "WRONG",
            },
        )
    )
    assert resp.status_code == 403


# ── Form-field as uploaded file part ─────────────────────────────────


def test_post_with_csrf_form_field_as_uploadfile_is_refused():
    # If the `csrf_token` multipart part arrives as a file upload, `form.get`
    # returns an UploadFile; compare_digest would crash on it. Middleware must
    # treat the non-string value as a missing token and refuse with 403.
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


def test_csrf_header_path_accepts_matching_token() -> None:
    app = Veloce()
    app.add_middleware(CSRFMiddleware(cookie_secure=False))

    @app.get("/seed")
    async def seed() -> dict:
        return {"ok": True}

    @app.post("/submit")
    async def submit() -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/seed")
        token = client.cookies.get("csrf_token")
        assert token, "CSRF cookie should have been minted"
        resp = client.post("/submit", headers={"X-CSRF-Token": token})
        assert resp.status_code == 200, resp.body
        assert resp.json() == {"ok": True}


# ── end to end through a client ───────────────────────────────
#
# Moved here from `test_security_middleware_e2e.py`, which covered three
# unrelated middleware subsystems end to end. These are that subsystem's.


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
