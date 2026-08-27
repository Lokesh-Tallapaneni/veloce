"""Query(), Path(), Header(), Cookie() parameter classes — handler injection."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Cookie, Header, Path, Query, Veloce


class TestParamClasses:
    async def test_query_with_validation(self):
        app = Veloce(openapi_url=None)

        @app.get("/items")
        async def items(page: int = Query(default=1, ge=1), limit: int = Query(default=10, le=100)):
            return {"page": page, "limit": limit}

        resp = await app.handle_request(make_request(path="/items", query_string="page=3&limit=20"))
        import orjson

        data = orjson.loads(resp.body)
        assert data["page"] == 3
        assert data["limit"] == 20

    async def test_query_default(self):
        app = Veloce(openapi_url=None)

        @app.get("/search")
        async def search(q: str = Query(default="")):
            return {"q": q}

        resp = await app.handle_request(make_request(path="/search"))
        import orjson

        assert orjson.loads(resp.body)["q"] == ""

    async def test_query_validation_error(self):
        app = Veloce(openapi_url=None)

        @app.get("/items")
        async def items(page: int = Query(default=1, ge=1)):
            return {"page": page}

        resp = await app.handle_request(make_request(path="/items", query_string="page=0"))
        assert resp.status_code == 422

    async def test_header_param(self):
        app = Veloce(openapi_url=None)

        @app.get("/check")
        async def check(x_token: str = Header(alias="x-token")):
            return {"token": x_token}

        resp = await app.handle_request(
            make_request(path="/check", headers={"x-token": "secret123"})
        )
        import orjson

        assert orjson.loads(resp.body)["token"] == "secret123"

    async def test_header_missing_required(self):
        app = Veloce(openapi_url=None)

        @app.get("/check")
        async def check(x_token: str = Header(alias="x-token")):
            return {"token": x_token}

        resp = await app.handle_request(make_request(path="/check"))
        assert resp.status_code == 422

    async def test_cookie_param(self):
        app = Veloce(openapi_url=None)

        @app.get("/me")
        async def me(session_id: str = Cookie(default=None)):
            return {"session": session_id}

        resp = await app.handle_request(
            make_request(path="/me", headers={"cookie": "session_id=abc123"})
        )
        import orjson

        assert orjson.loads(resp.body)["session"] == "abc123"

    async def test_path_param_class(self):
        app = Veloce(openapi_url=None)

        @app.get("/items/{item_id}")
        async def get_item(item_id: int = Path(ge=1)):
            return {"id": item_id}

        resp = await app.handle_request(make_request(path="/items/42"))
        import orjson

        assert orjson.loads(resp.body)["id"] == 42

    async def test_string_length_validation(self):
        app = Veloce(openapi_url=None)

        @app.get("/name")
        async def name(n: str = Query(min_length=2, max_length=10)):
            return {"name": n}

        resp = await app.handle_request(make_request(path="/name", query_string="n=a"))
        assert resp.status_code == 422

        resp = await app.handle_request(make_request(path="/name", query_string="n=alice"))
        assert resp.status_code == 200


class TestOptionalParams:
    async def test_optional_query(self):
        app = Veloce(openapi_url=None)

        @app.get("/search")
        async def search(q: str | None = None):
            return {"q": q}

        resp = await app.handle_request(make_request(path="/search"))
        import orjson

        assert orjson.loads(resp.body)["q"] is None

        resp = await app.handle_request(make_request(path="/search", query_string="q=test"))
        assert orjson.loads(resp.body)["q"] == "test"
