"""A security scheme describes itself, so a custom one is published like a built-in.

The published document is a contract. A nine-branch `isinstance` cascade decided
what a scheme looked like, and returned nothing for anything it did not
recognise - so a user's own `SecurityScheme` subclass authenticated correctly at
runtime and appeared in the schema as an endpoint needing no credential. Swagger
UI's Authorize button, generated SDKs and the route-contract IR all read that.

A scheme that genuinely cannot describe itself is still not publishable, but it
is no longer silent: the build warns rather than asserting the route is open.
"""

from __future__ import annotations

import warnings

import pytest

from tests._openapi import document
from veloce import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    Depends,
    HTTPBasic,
    HTTPBearer,
    HTTPDigest,
    Security,
    Veloce,
)
from veloce.exceptions import Unauthorized
from veloce.security.api_key import _APIKeyBase
from veloce.security.base import SecurityScheme
from veloce.security.oauth2 import (
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    OpenIdConnect,
)
from veloce.testclient import TestClient


class CertAuth(SecurityScheme):
    """A scheme written outside the framework, the documented way."""

    __slots__ = ()

    async def __call__(self, request):
        value = request.headers.get("x-cert")
        if not value:
            raise Unauthorized("no cert")
        return {"cert": value}

    def openapi_scheme(self):
        return {"type": "apiKey", "in": "header", "name": "X-Cert"}


class UndescribedAuth(SecurityScheme):
    """A scheme that guards a route but says nothing about itself."""

    __slots__ = ()

    async def __call__(self, request):
        return {"user": 1}


# ── the defect ───────────────────────────────────────────────────────


def test_a_custom_scheme_is_published_not_dropped():
    """The defect: this route was published with no security requirement."""
    app = Veloce()

    @app.get("/secret")
    async def secret(user=Depends(CertAuth())):
        return user

    schema = document(app)
    assert schema["components"]["securitySchemes"]["CertAuth"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Cert",
    }
    assert schema["paths"]["/secret"]["get"]["security"] == [{"CertAuth": []}]


def test_the_document_agrees_with_what_the_route_actually_does():
    """The property that matters: enforced at runtime, stated in the schema."""
    app = Veloce()

    @app.get("/secret")
    async def secret(user=Depends(CertAuth())):
        return user

    client = TestClient(app)
    assert client.get("/secret").status_code == 401
    assert client.get("/secret", headers={"x-cert": "abc"}).status_code == 200
    assert document(app)["paths"]["/secret"]["get"]["security"] == [{"CertAuth": []}]


def test_a_custom_scheme_is_published_like_a_built_in():
    app = Veloce()

    @app.get("/a")
    async def a(u=Depends(CertAuth())):
        return u

    @app.get("/b")
    async def b(u=Depends(HTTPBearer())):
        return {}

    paths = document(app)["paths"]
    assert paths["/a"]["get"]["security"] == [{"CertAuth": []}]
    assert paths["/b"]["get"]["security"] == [{"HTTPBearer": []}]


# ── every built-in still describes itself correctly ──────────────────


@pytest.mark.parametrize(
    ("scheme", "expected"),
    [
        (APIKeyHeader(name="X-Key"), {"type": "apiKey", "in": "header", "name": "X-Key"}),
        (APIKeyQuery(name="k"), {"type": "apiKey", "in": "query", "name": "k"}),
        (APIKeyCookie(name="c"), {"type": "apiKey", "in": "cookie", "name": "c"}),
        (HTTPBasic(), {"type": "http", "scheme": "basic"}),
        (HTTPBearer(), {"type": "http", "scheme": "bearer"}),
        (HTTPDigest(realm="r"), {"type": "http", "scheme": "digest"}),
        (
            OpenIdConnect(openIdConnectUrl="https://x/.well-known/openid-configuration"),
            {
                "type": "openIdConnect",
                "openIdConnectUrl": "https://x/.well-known/openid-configuration",
            },
        ),
    ],
)
def test_a_built_in_scheme_describes_itself(scheme, expected):
    assert scheme.openapi_scheme() == expected


def test_the_password_flow_carries_its_token_url_and_scopes():
    scheme = OAuth2PasswordBearer(token_url="/token", scopes={"read": "Read"})
    assert scheme.openapi_scheme() == {
        "type": "oauth2",
        "flows": {"password": {"tokenUrl": "/token", "scopes": {"read": "Read"}}},
    }


def test_the_authorization_code_flow_omits_an_unset_refresh_url():
    scheme = OAuth2AuthorizationCodeBearer(
        authorizationUrl="https://a/authorize", tokenUrl="https://a/token"
    )
    flow = scheme.openapi_scheme()["flows"]["authorizationCode"]
    assert "refreshUrl" not in flow
    assert flow["authorizationUrl"] == "https://a/authorize"


def test_the_authorization_code_flow_keeps_a_set_refresh_url():
    scheme = OAuth2AuthorizationCodeBearer(
        authorizationUrl="https://a/authorize",
        tokenUrl="https://a/token",
        refreshUrl="https://a/refresh",
    )
    flow = scheme.openapi_scheme()["flows"]["authorizationCode"]
    assert flow["refreshUrl"] == "https://a/refresh"


def test_oauth2_scopes_reach_the_operation_requirement():
    app = Veloce()
    scheme = OAuth2PasswordBearer(token_url="/token", scopes={"read": "Read", "write": "Write"})

    @app.get("/scoped")
    async def scoped(user=Security(scheme, scopes=["read"])):
        return {}

    assert document(app)["paths"]["/scoped"]["get"]["security"] == [
        {"OAuth2PasswordBearer": ["read"]}
    ]


# ── the undescribable case: not published, but not silent ────────────


def test_a_scheme_that_cannot_describe_itself_warns():
    """Publishing nothing asserts the route is open - that must not be quiet."""
    app = Veloce()

    @app.get("/x")
    async def x(user=Depends(UndescribedAuth())):
        return user

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        document(app)
    messages = [str(w.message) for w in caught]
    assert any("UndescribedAuth" in m and "openapi_scheme()" in m for m in messages)


def test_an_ordinary_dependency_does_not_warn():
    """Only a SecurityScheme claims to guard a route; a plain dep does not."""
    app = Veloce()

    def plain_dep():
        return {"ok": True}

    @app.get("/x")
    async def x(value=Depends(plain_dep)):
        return value

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        document(app)
    assert [str(w.message) for w in caught if "openapi_scheme" in str(w.message)] == []


def test_a_described_scheme_does_not_warn():
    app = Veloce()

    @app.get("/x")
    async def x(u=Depends(CertAuth())):
        return u

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        document(app)
    assert [str(w.message) for w in caught if "openapi_scheme" in str(w.message)] == []


# ── edges ────────────────────────────────────────────────────────────


def test_a_scheme_returning_an_empty_object_is_treated_as_undescribed():
    """An empty dict is not a valid Security Scheme Object."""

    class EmptyAuth(SecurityScheme):
        __slots__ = ()

        async def __call__(self, request):
            return {}

        def openapi_scheme(self):
            return {}

    app = Veloce()

    @app.get("/x")
    async def x(u=Depends(EmptyAuth())):
        return u

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = document(app)
    assert "security" not in schema["paths"]["/x"]["get"]
    assert any("EmptyAuth" in str(w.message) for w in caught)


def test_two_routes_sharing_a_scheme_share_one_component_entry():
    app = Veloce()
    scheme = CertAuth()

    @app.get("/a")
    async def a(u=Depends(scheme)):
        return u

    @app.get("/b")
    async def b(u=Depends(scheme)):
        return u

    schema = document(app)
    assert list(schema["components"]["securitySchemes"]) == ["CertAuth"]
    assert schema["paths"]["/a"]["get"]["security"] == [{"CertAuth": []}]
    assert schema["paths"]["/b"]["get"]["security"] == [{"CertAuth": []}]


def test_a_scheme_reached_through_a_nested_dependency_is_published():
    """The walk descends into ordinary dependencies to find the scheme."""
    app = Veloce()

    def current_user(cert=Depends(CertAuth())):
        return cert

    @app.get("/deep")
    async def deep(user=Depends(current_user)):
        return user

    assert document(app)["paths"]["/deep"]["get"]["security"] == [{"CertAuth": []}]


def test_an_api_key_subclass_must_declare_its_openapi_location():
    """Paired with `_source_attr`: an undeclared location publishes an empty `in`."""

    with pytest.raises(TypeError, match="_openapi_in"):

        class Broken(_APIKeyBase):
            __slots__ = ()
            _source_attr = "headers"


def test_an_unguarded_route_publishes_no_security_key():
    app = Veloce()

    @app.get("/open")
    async def open_route():
        return {}

    assert "security" not in document(app)["paths"]["/open"]["get"]
