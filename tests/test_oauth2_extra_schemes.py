"""OAuth2AuthorizationCodeBearer + OpenIdConnect security schemes."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import (
    HTTPException,
    OAuth2AuthorizationCodeBearer,
    OpenIdConnect,
    Request,
    Security,
    Veloce,
)


def _req(headers: dict | None = None) -> Request:
    return make_request(method="GET", path="/x", query_string="", headers=headers or {}, body=b"")


# ── Authorization-Code bearer ────────────────────────────────────────


def test_auth_code_extracts_bearer_token():
    scheme = OAuth2AuthorizationCodeBearer(
        authorizationUrl="https://auth.example.com/authorize",
        tokenUrl="https://auth.example.com/token",
        scopes={"read": "Read"},
    )
    token = scheme(_req({"authorization": "Bearer abc123"}))
    assert token == "abc123"


def test_auth_code_auto_error_raises_401():
    scheme = OAuth2AuthorizationCodeBearer(authorizationUrl="x", tokenUrl="y", auto_error=True)
    with pytest.raises(HTTPException) as exc:
        scheme(_req())
    assert exc.value.status_code == 401
    assert exc.value.headers["WWW-Authenticate"] == "Bearer"


def test_auth_code_auto_error_false_returns_none():
    scheme = OAuth2AuthorizationCodeBearer(authorizationUrl="x", tokenUrl="y", auto_error=False)
    assert scheme(_req()) is None


# ── OpenIdConnect ────────────────────────────────────────────────────


def test_openid_connect_extracts_bearer_token():
    scheme = OpenIdConnect(openIdConnectUrl="https://example.com/.well-known/openid-configuration")
    token = scheme(_req({"authorization": "Bearer xyz"}))
    assert token == "xyz"


def test_openid_connect_auto_error_raises_401():
    scheme = OpenIdConnect(openIdConnectUrl="x", auto_error=True)
    with pytest.raises(HTTPException) as exc:
        scheme(_req())
    assert exc.value.status_code == 401


# ── OpenAPI emission ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_code_bearer_emits_openapi_security_scheme():
    app = Veloce(debug=True, openapi_url="/openapi.json")
    oauth2 = OAuth2AuthorizationCodeBearer(
        authorizationUrl="https://auth.example.com/authorize",
        tokenUrl="https://auth.example.com/token",
        refreshUrl="https://auth.example.com/refresh",
        scopes={"read:items": "Read items"},
    )

    @app.get("/items")
    async def get_items(token=Security(oauth2)):
        return []

    from veloce.contrib.openapi import get_openapi_schema

    schema = get_openapi_schema(app)
    schemes = schema["components"]["securitySchemes"]
    assert "OAuth2AuthorizationCodeBearer" in schemes
    sd = schemes["OAuth2AuthorizationCodeBearer"]
    assert sd["type"] == "oauth2"
    flow = sd["flows"]["authorizationCode"]
    assert flow["authorizationUrl"] == "https://auth.example.com/authorize"
    assert flow["tokenUrl"] == "https://auth.example.com/token"
    assert flow["refreshUrl"] == "https://auth.example.com/refresh"
    assert flow["scopes"] == {"read:items": "Read items"}


@pytest.mark.asyncio
async def test_openid_connect_emits_openapi_security_scheme():
    app = Veloce(debug=True, openapi_url="/openapi.json")
    oidc = OpenIdConnect(openIdConnectUrl="https://ex.com/.well-known/openid-configuration")

    @app.get("/items")
    async def get_items(token=Security(oidc)):
        return []

    from veloce.contrib.openapi import get_openapi_schema

    schema = get_openapi_schema(app)
    schemes = schema["components"]["securitySchemes"]
    assert "OpenIdConnect" in schemes
    sd = schemes["OpenIdConnect"]
    assert sd["type"] == "openIdConnect"
    assert sd["openIdConnectUrl"] == "https://ex.com/.well-known/openid-configuration"
