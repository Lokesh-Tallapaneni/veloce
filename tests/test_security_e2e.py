"""End-to-end TestClient coverage for the security-module fixes.

Exercises the real request/response path (radix router + DI resolver +
security scheme `__call__`) rather than calling the schemes directly.
"""

from __future__ import annotations

from veloce import (
    APIKeyHeader,
    Depends,
    HTTPBearer,
    HTTPDigest,
    OAuth2PasswordRequestFormStrict,
    Veloce,
)
from veloce.testclient import TestClient

# ── Digest: default algorithm + escaped-quote parse ───────────────────


def test_digest_default_challenge_uses_sha256():
    """GET without Authorization returns 401 + WWW-Authenticate using SHA-256."""
    app = Veloce(openapi_url=None)
    digest = HTTPDigest(realm="testrealm@example.com")

    @app.get("/protected")
    async def protected(creds=Depends(digest)):
        return {"user": creds.username}

    with TestClient(app) as client:
        resp = client.get("/protected")

    assert resp.status_code == 401
    wwwa = resp.headers["www-authenticate"]
    assert wwwa.startswith("Digest ")
    assert "algorithm=SHA-256" in wwwa
    assert "algorithm=MD5" not in wwwa


def test_digest_escaped_quote_preserved_through_request():
    """`\\"` in a quoted field decodes to a literal `"` without corrupting the
    preceding literal backslash. Verifies _unescape_quoted single-pass walk
    survives the full request flow.
    """
    app = Veloce(openapi_url=None)
    digest = HTTPDigest(realm="r", auto_error=False)

    @app.get("/who")
    async def who(creds=Depends(digest)):
        return {"username": creds.username, "realm": creds.realm}

    # Wire form: username="a\\\"b" -> a, literal `\`, literal `"`, b.
    header = 'Digest username="a\\\\\\"b", realm="c:\\\\path", response="x"'
    with TestClient(app) as client:
        resp = client.get("/who", headers={"Authorization": header})

    assert resp.status_code == 200
    assert resp.json() == {"username": 'a\\"b', "realm": "c:\\path"}


# ── OAuth2 strict grant_type ──────────────────────────────────────────


def _token_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.post("/token")
    async def token(form: OAuth2PasswordRequestFormStrict = Depends()):
        return {
            "grant_type": form.grant_type,
            "username": form.username,
            "password": form.password,
        }

    return app


def test_oauth2_strict_missing_grant_type_is_422():
    with TestClient(_token_app()) as client:
        resp = client.post("/token", data={"username": "u", "password": "p"})
    assert resp.status_code == 422


def test_oauth2_strict_wrong_grant_type_is_422():
    with TestClient(_token_app()) as client:
        resp = client.post(
            "/token",
            data={"grant_type": "bearer", "username": "u", "password": "p"},
        )
    assert resp.status_code == 422


def test_oauth2_strict_correct_grant_type_succeeds():
    with TestClient(_token_app()) as client:
        resp = client.post(
            "/token",
            data={"grant_type": "password", "username": "u", "password": "p"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"grant_type": "password", "username": "u", "password": "p"}


# ── API key empty / whitespace ────────────────────────────────────────


def _apikey_app() -> Veloce:
    app = Veloce(openapi_url=None)
    api_key = APIKeyHeader(name="X-API-Key")

    @app.get("/k")
    async def k(key: str = Depends(api_key)):
        return {"key": key}

    return app


def test_api_key_empty_header_value_rejected():
    with TestClient(_apikey_app()) as client:
        resp = client.get("/k", headers={"X-API-Key": ""})
    assert resp.status_code == 401


def test_api_key_whitespace_only_header_rejected():
    with TestClient(_apikey_app()) as client:
        resp = client.get("/k", headers={"X-API-Key": "    "})
    assert resp.status_code == 401


# ── Bearer SP/HTAB stripping, NBSP retention ──────────────────────────


def _bearer_app() -> Veloce:
    app = Veloce(openapi_url=None)
    bearer = HTTPBearer()

    @app.get("/b")
    async def b(token: str = Depends(bearer)):
        return {"token": token}

    return app


def test_bearer_leading_sp_htab_stripped():
    with TestClient(_bearer_app()) as client:
        resp = client.get("/b", headers={"Authorization": "Bearer    token123"})
    assert resp.status_code == 200
    assert resp.json() == {"token": "token123"}


def test_bearer_trailing_sp_htab_stripped():
    with TestClient(_bearer_app()) as client:
        resp = client.get("/b", headers={"Authorization": "Bearer token123   "})
    assert resp.status_code == 200
    assert resp.json() == {"token": "token123"}


def test_bearer_nbsp_retained_in_token():
    """RFC 6750 §2.1 + RFC 7235: only SP/HTAB are stripped; NBSP (\\xa0) stays."""
    nbsp = "\xa0"
    token = f"abc{nbsp}def"
    with TestClient(_bearer_app()) as client:
        resp = client.get("/b", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"token": token}
