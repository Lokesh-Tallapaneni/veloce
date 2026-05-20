"""OpenAPI security scheme emission (O10)."""

from __future__ import annotations

from veloce import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    Depends,
    HTTPBasic,
    HTTPBearer,
    OAuth2PasswordBearer,
    Security,
    Veloce,
)
from veloce.testclient import TestClient


def _spec(app: Veloce) -> dict:
    return TestClient(app).get("/openapi.json").json()


# ── OAuth2 Password Bearer ────────────────────────────────────────────


def test_oauth2_password_bearer_emits_scheme():
    app = Veloce(debug=True)
    oauth = OAuth2PasswordBearer(
        token_url="/token",
        scopes={"read": "Read access", "write": "Write access"},
    )

    @app.get("/me")
    async def me(token: str = Security(oauth, scopes=["read"])):
        return {"token": token}

    spec = _spec(app)
    schemes = spec["components"]["securitySchemes"]
    assert "OAuth2PasswordBearer" in schemes
    scheme = schemes["OAuth2PasswordBearer"]
    assert scheme["type"] == "oauth2"
    assert scheme["flows"]["password"]["tokenUrl"] == "/token"
    assert scheme["flows"]["password"]["scopes"] == {
        "read": "Read access",
        "write": "Write access",
    }
    # Operation declares the scope it requires.
    op = spec["paths"]["/me"]["get"]
    assert op["security"] == [{"OAuth2PasswordBearer": ["read"]}]


# ── HTTP Bearer / Basic ───────────────────────────────────────────────


def test_http_bearer_emits_scheme():
    app = Veloce(debug=True)
    bearer = HTTPBearer()

    @app.get("/x")
    async def x(token: str = Security(bearer)):
        return {}

    spec = _spec(app)
    assert spec["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert spec["paths"]["/x"]["get"]["security"] == [{"HTTPBearer": []}]


def test_http_basic_emits_scheme():
    app = Veloce(debug=True)
    basic = HTTPBasic(realm="api")

    @app.get("/x")
    async def x(creds=Security(basic)):
        return {}

    spec = _spec(app)
    assert spec["components"]["securitySchemes"]["HTTPBasic"] == {
        "type": "http",
        "scheme": "basic",
    }


# ── API Key (header / query / cookie) ─────────────────────────────────


def test_apikey_header_emits_scheme():
    app = Veloce(debug=True)
    api_key = APIKeyHeader(name="X-API-Key")

    @app.get("/x")
    async def x(key: str = Security(api_key)):
        return {}

    schemes = _spec(app)["components"]["securitySchemes"]
    assert schemes["APIKeyHeader"] == {"type": "apiKey", "in": "header", "name": "X-API-Key"}


def test_apikey_query_emits_scheme():
    app = Veloce(debug=True)
    api_key = APIKeyQuery(name="api_key")

    @app.get("/x")
    async def x(key: str = Security(api_key)):
        return {}

    assert _spec(app)["components"]["securitySchemes"]["APIKeyQuery"] == {
        "type": "apiKey",
        "in": "query",
        "name": "api_key",
    }


def test_apikey_cookie_emits_scheme():
    app = Veloce(debug=True)
    api_key = APIKeyCookie(name="session")

    @app.get("/x")
    async def x(key: str = Security(api_key)):
        return {}

    assert _spec(app)["components"]["securitySchemes"]["APIKeyCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "session",
    }


# ── Multiple schemes / dedup ──────────────────────────────────────────


def test_multiple_routes_share_one_scheme_entry():
    """Two routes using the same scheme produce ONE
    components.securitySchemes entry — not duplicates."""
    app = Veloce(debug=True)
    bearer = HTTPBearer()

    @app.get("/a")
    async def a(t: str = Security(bearer)):
        return {}

    @app.get("/b")
    async def b(t: str = Security(bearer)):
        return {}

    schemes = _spec(app)["components"]["securitySchemes"]
    assert list(schemes.keys()) == ["HTTPBearer"]


def test_no_security_means_no_security_section():
    """A route with no Security() must not produce a security entry."""
    app = Veloce(debug=True)

    @app.get("/x")
    async def x():
        return {}

    spec = _spec(app)
    op = spec["paths"]["/x"]["get"]
    assert "security" not in op
    # And no securitySchemes component if nothing uses it.
    assert "securitySchemes" not in spec.get("components", {})


def test_plain_depends_does_not_register_a_scheme():
    """Plain `Depends(callable)` shouldn't be confused for a security scheme."""
    app = Veloce(debug=True)

    def get_user():
        return {"id": 1}

    @app.get("/me")
    async def me(user=Depends(get_user)):
        return user

    spec = _spec(app)
    assert "securitySchemes" not in spec.get("components", {})
    assert "security" not in spec["paths"]["/me"]["get"]


def test_security_nested_inside_depends_still_collected():
    """A Security() reached transitively through Depends still produces
    a scheme entry."""
    app = Veloce(debug=True)
    bearer = HTTPBearer()

    def get_current_user(token: str = Security(bearer)):
        return {"token": token}

    @app.get("/me")
    async def me(user=Depends(get_current_user)):
        return user

    spec = _spec(app)
    assert "HTTPBearer" in spec["components"]["securitySchemes"]
    assert spec["paths"]["/me"]["get"]["security"] == [{"HTTPBearer": []}]
