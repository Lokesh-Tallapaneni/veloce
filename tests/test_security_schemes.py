"""HTTP/API-key/OAuth2 security schemes as Depends() dependencies."""

from __future__ import annotations

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
        import orjson

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

        import base64

        creds = base64.b64encode(b"admin:secret").decode()
        resp = await app.handle_request(
            make_request(path="/admin", headers={"authorization": f"Basic {creds}"})
        )
        assert resp.status_code == 200
        import orjson

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
        import orjson

        assert orjson.loads(resp.body)["token"] == "jwt.token.here"
