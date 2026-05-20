"""Tests for the Veloce application — full integration tests."""

import pytest
from pydantic import BaseModel

from veloce import Depends, HTMLResponse, HTTPException, JSONResponse, Request, Router, Veloce
from veloce.http.response import RedirectResponse, Response
from veloce.middleware import CORSMiddleware


@pytest.fixture
def app():
    return Veloce(debug=True)


def make_request(method="GET", path="/", headers=None, body=b"", query_string="") -> Request:
    return Request(
        method=method,
        path=path,
        query_string=query_string,
        headers=headers or {},
        body=body,
    )


class TestBasicRoutes:
    @pytest.mark.asyncio
    async def test_get_dict_response(self, app):
        @app.get("/")
        async def index(request: Request):
            return {"hello": "world"}

        response = await app.handle_request(make_request())
        assert response.status_code == 200
        assert b'"hello"' in response.body

    @pytest.mark.asyncio
    async def test_get_string_response(self, app):
        @app.get("/text")
        async def text(request: Request):
            return "hello"

        response = await app.handle_request(make_request(path="/text"))
        assert response.status_code == 200
        assert response.body == b"hello"

    @pytest.mark.asyncio
    async def test_html_response(self, app):
        @app.get("/html")
        async def html(request: Request):
            return HTMLResponse("<h1>Hi</h1>")

        response = await app.handle_request(make_request(path="/html"))
        assert response.status_code == 200
        assert b"<h1>Hi</h1>" in response.body

    @pytest.mark.asyncio
    async def test_404(self, app):
        response = await app.handle_request(make_request(path="/nope"))
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_method_not_allowed(self, app):
        @app.get("/only-get")
        async def only_get(request: Request):
            return "ok"

        response = await app.handle_request(make_request(method="POST", path="/only-get"))
        assert response.status_code == 405


class TestPathParams:
    @pytest.mark.asyncio
    async def test_string_param(self, app):
        @app.get("/hello/{name}")
        async def hello(name: str):
            return {"name": name}

        response = await app.handle_request(make_request(path="/hello/alice"))
        assert b"alice" in response.body

    @pytest.mark.asyncio
    async def test_int_param(self, app):
        @app.get("/items/{item_id}")
        async def get_item(item_id: int):
            return {"id": item_id, "type": type(item_id).__name__}

        response = await app.handle_request(make_request(path="/items/42"))
        assert response.status_code == 200
        assert b"42" in response.body


class TestQueryParams:
    @pytest.mark.asyncio
    async def test_query_params(self, app):
        @app.get("/search")
        async def search(q: str = "", page: int = 1):
            return {"q": q, "page": page}

        response = await app.handle_request(
            make_request(path="/search", query_string="q=test&page=3")
        )
        assert response.status_code == 200
        assert b"test" in response.body

    @pytest.mark.asyncio
    async def test_default_query_params(self, app):
        @app.get("/list")
        async def list_items(limit: int = 10):
            return {"limit": limit}

        response = await app.handle_request(make_request(path="/list"))
        assert response.status_code == 200
        assert b"10" in response.body


class TestRequestBody:
    @pytest.mark.asyncio
    async def test_pydantic_body(self, app):
        class Item(BaseModel):
            name: str
            price: float

        @app.post("/items")
        async def create_item(item: Item):
            return {"name": item.name, "price": item.price}

        import orjson

        body = orjson.dumps({"name": "Widget", "price": 9.99})
        response = await app.handle_request(
            make_request(
                method="POST",
                path="/items",
                body=body,
                headers={"content-type": "application/json"},
            )
        )
        assert response.status_code == 200
        assert b"Widget" in response.body

    @pytest.mark.asyncio
    async def test_invalid_body_validation(self, app):
        class Item(BaseModel):
            name: str
            price: float

        @app.post("/items")
        async def create_item(item: Item):
            return item.model_dump()

        response = await app.handle_request(
            make_request(
                method="POST",
                path="/items",
                body=b'{"name": "test"}',  # missing price
                headers={"content-type": "application/json"},
            )
        )
        assert response.status_code == 422


class TestDependencyInjection:
    @pytest.mark.asyncio
    async def test_sync_dependency(self, app):
        def get_db():
            return {"connected": True}

        @app.get("/db")
        async def check_db(db=Depends(get_db)):
            return db

        response = await app.handle_request(make_request(path="/db"))
        assert response.status_code == 200
        assert b"true" in response.body

    @pytest.mark.asyncio
    async def test_async_dependency(self, app):
        async def get_user(request: Request):
            return {"user": "admin"}

        @app.get("/user")
        async def get_current(user=Depends(get_user)):
            return user

        response = await app.handle_request(make_request(path="/user"))
        assert b"admin" in response.body

    @pytest.mark.asyncio
    async def test_dependency_raises(self, app):
        def auth_required(request: Request):
            raise HTTPException(401, "Unauthorized")

        @app.get("/protected")
        async def protected(user=Depends(auth_required)):
            return {"secret": True}

        response = await app.handle_request(make_request(path="/protected"))
        assert response.status_code == 401


class TestMiddleware:
    @pytest.mark.asyncio
    async def test_cors_preflight(self, app):
        app.add_middleware(CORSMiddleware(allow_origins=["*"]))

        @app.get("/api")
        async def api(request: Request):
            return {"ok": True}

        response = await app.handle_request(
            make_request(
                method="OPTIONS",
                path="/api",
                headers={"origin": "http://example.com"},
            )
        )
        assert response.status_code == 204
        assert "Access-Control-Allow-Origin" in response.headers

    @pytest.mark.asyncio
    async def test_cors_response_headers(self, app):
        app.add_middleware(CORSMiddleware(allow_origins=["http://localhost:3000"]))

        @app.get("/data")
        async def data(request: Request):
            return {"data": True}

        response = await app.handle_request(
            make_request(
                path="/data",
                headers={"origin": "http://localhost:3000"},
            )
        )
        assert response.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"


class TestExceptionHandlers:
    @pytest.mark.asyncio
    async def test_custom_exception_handler(self, app):
        @app.exception_handler(HTTPException)
        async def custom_handler(request: Request, exc: HTTPException):
            return JSONResponse({"custom_error": exc.detail}, status_code=exc.status_code)

        @app.get("/fail")
        async def fail(request: Request):
            raise HTTPException(418, "I'm a teapot")

        response = await app.handle_request(make_request(path="/fail"))
        assert response.status_code == 418
        assert b"custom_error" in response.body

    @pytest.mark.asyncio
    async def test_unhandled_exception_debug(self, app):
        @app.get("/crash")
        async def crash(request: Request):
            raise ValueError("boom")

        response = await app.handle_request(make_request(path="/crash"))
        assert response.status_code == 500
        assert b"boom" in response.body  # debug mode shows traceback


class TestRouterInclusion:
    @pytest.mark.asyncio
    async def test_included_router(self, app):
        api = Router(prefix="/api/v1")

        @api.get("/items")
        async def items(request: Request):
            return [{"id": 1}]

        app.include_router(api)

        response = await app.handle_request(make_request(path="/api/v1/items"))
        assert response.status_code == 200


class TestResponseTypes:
    @pytest.mark.asyncio
    async def test_redirect(self, app):
        @app.get("/old")
        async def old(request: Request):
            return RedirectResponse("/new")

        response = await app.handle_request(make_request(path="/old"))
        assert response.status_code == 307
        assert response.headers["Location"] == "/new"

    @pytest.mark.asyncio
    async def test_pydantic_model_response(self, app):
        class Item(BaseModel):
            name: str
            price: float

        @app.get("/item")
        async def get_item(request: Request):
            return Item(name="Widget", price=9.99)

        response = await app.handle_request(make_request(path="/item"))
        assert response.status_code == 200
        assert b"Widget" in response.body

    def test_response_encoding(self):
        r = Response(status_code=200, body=b"hello", content_type="text/plain")
        encoded = r.encode()
        assert b"HTTP/1.1 200 OK" in encoded
        assert b"Content-Length: 5" in encoded
        assert b"hello" in encoded

    def test_cookie_setting(self):
        r = Response(status_code=200, body=b"ok")
        r.set_cookie("session", "abc123", httponly=True, samesite="Lax")
        assert "Set-Cookie" in r.headers
        assert "HttpOnly" in r.headers["Set-Cookie"]


class TestSyncHandlers:
    @pytest.mark.asyncio
    async def test_sync_handler(self, app):
        @app.get("/sync")
        def sync_handler(request: Request):
            return {"sync": True}

        response = await app.handle_request(make_request(path="/sync"))
        assert response.status_code == 200
        assert b"sync" in response.body
