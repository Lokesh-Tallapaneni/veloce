"""HTTP/API-key/OAuth2 security schemes as Depends() dependencies."""

from __future__ import annotations

import base64

import orjson
import pytest

from tests.conftest import make_request
from veloce import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    Depends,
    HTTPBasic,
    HTTPBearer,
    OAuth2PasswordBearer,
    Veloce,
)
from veloce.testclient import TestClient


class TestSecurity:
    async def test_http_bearer(self):
        app = Veloce(openapi_url=None)
        security = HTTPBearer()

        @app.get("/protected")
        async def protected(token=Depends(security)):
            return {"token": token}

        resp = await app.handle_request(
            make_request(path="/protected", headers={"authorization": "Bearer mytoken123"})
        )
        assert resp.status_code == 200
        assert orjson.loads(resp.body)["token"] == "mytoken123"

    async def test_http_bearer_missing(self):
        app = Veloce(openapi_url=None)
        security = HTTPBearer()

        @app.get("/protected")
        async def protected(token=Depends(security)):
            return {"token": token}

        resp = await app.handle_request(make_request(path="/protected"))
        assert resp.status_code == 401

    async def test_http_basic(self):
        app = Veloce(openapi_url=None)
        security = HTTPBasic()

        @app.get("/admin")
        async def admin(credentials=Depends(security)):
            return {"user": credentials.username}

        creds = base64.b64encode(b"admin:secret").decode()
        resp = await app.handle_request(
            make_request(path="/admin", headers={"authorization": f"Basic {creds}"})
        )
        assert resp.status_code == 200
        assert orjson.loads(resp.body)["user"] == "admin"

    async def test_api_key_header(self):
        app = Veloce(openapi_url=None)
        api_key = APIKeyHeader(name="X-API-Key")

        @app.get("/data")
        async def data(key=Depends(api_key)):
            return {"key": key}

        resp = await app.handle_request(
            make_request(path="/data", headers={"x-api-key": "secret123"})
        )
        assert resp.status_code == 200

    async def test_api_key_query(self):
        app = Veloce(openapi_url=None)
        api_key = APIKeyQuery(name="api_key")

        @app.get("/data")
        async def data(key=Depends(api_key)):
            return {"key": key}

        resp = await app.handle_request(make_request(path="/data", query_string="api_key=mykey"))
        assert resp.status_code == 200

    async def test_api_key_cookie(self):
        app = Veloce(openapi_url=None)
        api_key = APIKeyCookie(name="token")

        @app.get("/data")
        async def data(key=Depends(api_key)):
            return {"key": key}

        resp = await app.handle_request(
            make_request(path="/data", headers={"cookie": "token=cookiekey"})
        )
        assert resp.status_code == 200

    async def test_oauth2_password_bearer(self):
        app = Veloce(openapi_url=None)
        oauth2 = OAuth2PasswordBearer(token_url="/token")

        @app.get("/me")
        async def me(token=Depends(oauth2)):
            return {"token": token}

        resp = await app.handle_request(
            make_request(path="/me", headers={"authorization": "Bearer jwt.token.here"})
        )
        assert resp.status_code == 200
        assert orjson.loads(resp.body)["token"] == "jwt.token.here"


# ── the published scheme is the one the server accepts ───────────────
#
# `HTTPBearer(scheme_name="Token")` changed what `__call__` matches without
# changing what `openapi_scheme()` told clients to send.


def test_the_default_scheme_is_bearer():
    assert HTTPBearer().openapi_scheme() == {"type": "http", "scheme": "bearer"}


def test_a_custom_scheme_reaches_the_document():
    """The defect: this published `bearer` while the server matched `Token`."""
    assert HTTPBearer(scheme_name="Token").openapi_scheme() == {
        "type": "http",
        "scheme": "token",
    }


def test_the_published_scheme_is_lower_cased():
    """OpenAPI 3.1 names the IANA registry entry, whose entries are lower-case."""
    assert HTTPBearer(scheme_name="BEARER").openapi_scheme()["scheme"] == "bearer"


@pytest.mark.parametrize("scheme", ["Bearer", "Token", "DPoP"])
def test_the_document_and_the_runtime_agree(scheme):
    """The property: what is published is what is accepted."""

    app = Veloce(openapi_url=None)
    guard = HTTPBearer(scheme_name=scheme)

    @app.get("/private")
    async def private(credential: str = Depends(guard)) -> dict:
        return {"token": credential}

    client = TestClient(app)
    published = guard.openapi_scheme()["scheme"]
    # A client following the document sends the scheme it names.
    accepted = client.get("/private", headers={"Authorization": f"{published} abc123"})
    assert accepted.status_code == 200
    assert accepted.json() == {"token": "abc123"}


def test_a_different_scheme_is_still_refused():
    """The negative: accepting anything would pass the test above vacuously."""

    app = Veloce(openapi_url=None)

    @app.get("/private")
    async def private(credential=Depends(HTTPBearer(scheme_name="Token"))) -> dict:
        return {}

    assert TestClient(app).get("/private", headers={"Authorization": "Bearer x"}).status_code == 401


def test_the_scheme_reaches_the_openapi_document():
    """End to end: through the generated document, not just the method."""

    app = Veloce(title="S", version="1.0.0")

    @app.get("/private")
    async def private(credential=Depends(HTTPBearer(scheme_name="Token"))) -> dict:
        return {}

    schemes = app.openapi()["components"]["securitySchemes"]
    assert any(entry.get("scheme") == "token" for entry in schemes.values())
