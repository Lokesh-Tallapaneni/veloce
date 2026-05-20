"""Tests for iteration-2 features — helpers, params, status_code, lifespan, etc."""

import enum

import pytest

from veloce import (
    BackgroundTasks,
    Cookie,
    Header,
    HTMLResponse,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    Veloce,
    abort,
    g,
    jsonify,
    make_response,
)
from veloce.http.request import Request as VRequest


def make_request(method="GET", path="/", headers=None, body=b"", query_string=""):
    return VRequest(
        method=method,
        path=path,
        query_string=query_string,
        headers=headers or {},
        body=body,
    )


# ═══════════════════════════════════════════════════════════════
# abort()
# ═══════════════════════════════════════════════════════════════


class TestAbort:
    def test_abort_raises(self):
        with pytest.raises(HTTPException) as exc_info:
            abort(404)
        assert exc_info.value.status_code == 404

    def test_abort_with_detail(self):
        with pytest.raises(HTTPException) as exc_info:
            abort(403, "Forbidden")
        assert exc_info.value.detail == "Forbidden"

    @pytest.mark.asyncio
    async def test_abort_in_handler(self):
        app = Veloce(openapi_url=None)

        @app.get("/fail")
        async def fail(request: Request):
            abort(418, "I'm a teapot")

        resp = await app.handle_request(make_request(path="/fail"))
        assert resp.status_code == 418


# ═══════════════════════════════════════════════════════════════
# jsonify()
# ═══════════════════════════════════════════════════════════════


class TestJsonify:
    def test_jsonify_kwargs(self):
        resp = jsonify(name="alice", age=30)
        assert resp.status_code == 200
        import orjson

        data = orjson.loads(resp.body)
        assert data["name"] == "alice"

    def test_jsonify_dict(self):
        resp = jsonify({"x": 1})
        import orjson

        assert orjson.loads(resp.body) == {"x": 1}

    def test_jsonify_list(self):
        resp = jsonify([1, 2, 3])
        import orjson

        assert orjson.loads(resp.body) == [1, 2, 3]


# ═══════════════════════════════════════════════════════════════
# make_response()
# ═══════════════════════════════════════════════════════════════


class TestMakeResponse:
    def test_make_response_string(self):
        resp = make_response("Hello", 201)
        assert resp.status_code == 201
        assert resp.body == b"Hello"

    def test_make_response_dict(self):
        resp = make_response({"ok": True}, 200)
        assert resp.status_code == 200
        import orjson

        assert orjson.loads(resp.body)["ok"] is True

    def test_make_response_bytes(self):
        resp = make_response(b"\x00\x01", 200)
        assert resp.body == b"\x00\x01"


# ═══════════════════════════════════════════════════════════════
# g object
# ═══════════════════════════════════════════════════════════════


class TestGObject:
    @pytest.mark.asyncio
    async def test_g_per_request(self):
        app = Veloce(openapi_url=None)

        @app.get("/set")
        async def set_g(request: Request):
            g.user = "alice"
            return {"user": g.user}

        @app.get("/get")
        async def get_g(request: Request):
            return {"user": g.get("user", "nobody")}

        # g is reset per request
        resp1 = await app.handle_request(make_request(path="/set"))
        import orjson

        assert orjson.loads(resp1.body)["user"] == "alice"

        resp2 = await app.handle_request(make_request(path="/get"))
        assert orjson.loads(resp2.body)["user"] == "nobody"

    def test_g_attribute_error(self):
        g._reset()
        with pytest.raises(AttributeError):
            _ = g.nonexistent

    def test_g_contains(self):
        g._reset()
        g.test_key = "val"
        assert "test_key" in g
        assert "missing" not in g

    def test_g_setdefault(self):
        g._reset()
        result = g.setdefault("counter", 0)
        assert result == 0
        g.counter = 5
        result = g.setdefault("counter", 0)
        assert result == 5

    def test_g_pop(self):
        g._reset()
        g.temp = "data"
        val = g.pop("temp")
        assert val == "data"
        assert "temp" not in g

    def test_g_delete(self):
        g._reset()
        g.to_delete = "x"
        del g.to_delete
        assert "to_delete" not in g


# ═══════════════════════════════════════════════════════════════
# Query(), Path(), Header(), Cookie() parameter classes
# ═══════════════════════════════════════════════════════════════


class TestParamClasses:
    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_query_default(self):
        app = Veloce(openapi_url=None)

        @app.get("/search")
        async def search(q: str = Query(default="")):
            return {"q": q}

        resp = await app.handle_request(make_request(path="/search"))
        import orjson

        assert orjson.loads(resp.body)["q"] == ""

    @pytest.mark.asyncio
    async def test_query_validation_error(self):
        app = Veloce(openapi_url=None)

        @app.get("/items")
        async def items(page: int = Query(default=1, ge=1)):
            return {"page": page}

        resp = await app.handle_request(make_request(path="/items", query_string="page=0"))
        assert resp.status_code == 422

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_header_missing_required(self):
        app = Veloce(openapi_url=None)

        @app.get("/check")
        async def check(x_token: str = Header(alias="x-token")):
            return {"token": x_token}

        resp = await app.handle_request(make_request(path="/check"))
        assert resp.status_code == 422

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_path_param_class(self):
        app = Veloce(openapi_url=None)

        @app.get("/items/{item_id}")
        async def get_item(item_id: int = Path(ge=1)):
            return {"id": item_id}

        resp = await app.handle_request(make_request(path="/items/42"))
        import orjson

        assert orjson.loads(resp.body)["id"] == 42

    @pytest.mark.asyncio
    async def test_string_length_validation(self):
        app = Veloce(openapi_url=None)

        @app.get("/name")
        async def name(n: str = Query(min_length=2, max_length=10)):
            return {"name": n}

        resp = await app.handle_request(make_request(path="/name", query_string="n=a"))
        assert resp.status_code == 422

        resp = await app.handle_request(make_request(path="/name", query_string="n=alice"))
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════
# Enum path parameters
# ═══════════════════════════════════════════════════════════════


class TestEnumParams:
    @pytest.mark.asyncio
    async def test_enum_path_param(self):
        class Color(str, enum.Enum):
            RED = "red"
            GREEN = "green"
            BLUE = "blue"

        app = Veloce(openapi_url=None)

        @app.get("/color/{color}")
        async def get_color(color: Color):
            return {"color": color.value}

        resp = await app.handle_request(make_request(path="/color/red"))
        import orjson

        assert orjson.loads(resp.body)["color"] == "red"

    @pytest.mark.asyncio
    async def test_enum_invalid(self):
        class Status(str, enum.Enum):
            ACTIVE = "active"
            INACTIVE = "inactive"

        app = Veloce(openapi_url=None)

        @app.get("/status/{status}")
        async def get_status(status: Status):
            return {"status": status.value}

        resp = await app.handle_request(make_request(path="/status/unknown"))
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════
# Optional parameter support
# ═══════════════════════════════════════════════════════════════


class TestOptionalParams:
    @pytest.mark.asyncio
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


# ═══════════════════════════════════════════════════════════════
# status_code in decorator
# ═══════════════════════════════════════════════════════════════


class TestStatusCodeDecorator:
    @pytest.mark.asyncio
    async def test_status_code_201(self):
        app = Veloce(openapi_url=None)

        @app.post("/items", status_code=201)
        async def create(request: Request):
            return {"id": 1}

        resp = await app.handle_request(make_request(method="POST", path="/items"))
        assert resp.status_code == 201


# ═══════════════════════════════════════════════════════════════
# response_class
# ═══════════════════════════════════════════════════════════════


class TestResponseClass:
    @pytest.mark.asyncio
    async def test_html_response_class(self):
        app = Veloce(openapi_url=None)

        @app.get("/page", response_class=HTMLResponse)
        async def page(request: Request):
            return "<h1>Hello</h1>"

        resp = await app.handle_request(make_request(path="/page"))
        assert b"<h1>Hello</h1>" in resp.body
        assert "text/html" in resp.content_type


# ═══════════════════════════════════════════════════════════════
# Tuple responses
# ═══════════════════════════════════════════════════════════════


class TestTupleResponse:
    @pytest.mark.asyncio
    async def test_tuple_body_status(self):
        app = Veloce(openapi_url=None)

        @app.post("/items")
        async def create(request: Request):
            return {"id": 1}, 201

        resp = await app.handle_request(make_request(method="POST", path="/items"))
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_tuple_body_status_headers(self):
        app = Veloce(openapi_url=None)

        @app.post("/items")
        async def create(request: Request):
            return {"id": 1}, 201, {"X-Custom": "value"}

        resp = await app.handle_request(make_request(method="POST", path="/items"))
        assert resp.status_code == 201
        assert resp.headers.get("X-Custom") == "value"


# ═══════════════════════════════════════════════════════════════
# delete_cookie
# ═══════════════════════════════════════════════════════════════


class TestDeleteCookie:
    def test_delete_cookie(self):
        resp = Response(status_code=200, body=b"ok")
        resp.delete_cookie("session")
        assert "Max-Age=0" in resp.headers["Set-Cookie"]

    def test_multiple_cookies(self):
        resp = Response(status_code=200, body=b"ok")
        resp.set_cookie("a", "1")
        resp.set_cookie("b", "2")
        cookie_header = resp.headers["Set-Cookie"]
        assert "a=1" in cookie_header
        assert "b=2" in cookie_header


# ═══════════════════════════════════════════════════════════════
# Lifespan context manager
# ═══════════════════════════════════════════════════════════════


class TestLifespan:
    @pytest.mark.asyncio
    async def test_lifespan_startup_shutdown(self):
        log = []

        async def lifespan(app):
            log.append("startup")
            app.state["db"] = {"connected": True}
            yield
            log.append("shutdown")

        from contextlib import asynccontextmanager

        app = Veloce(lifespan=asynccontextmanager(lifespan), openapi_url=None)

        await app._run_lifecycle("startup")
        assert "startup" in log
        assert app.state["db"]["connected"] is True

        await app._run_lifecycle("shutdown")
        assert "shutdown" in log


# ═══════════════════════════════════════════════════════════════
# app.state
# ═══════════════════════════════════════════════════════════════


class TestAppState:
    @pytest.mark.asyncio
    async def test_app_state(self):
        app = Veloce(openapi_url=None)
        app.state["config"] = {"debug": True}

        @app.get("/config")
        async def config(request: Request):
            return request.app.state["config"]

        resp = await app.handle_request(make_request(path="/config"))
        import orjson

        assert orjson.loads(resp.body)["debug"] is True


# ═══════════════════════════════════════════════════════════════
# BackgroundTasks injection
# ═══════════════════════════════════════════════════════════════


class TestBackgroundTasksInjection:
    @pytest.mark.asyncio
    async def test_background_tasks_injected(self):
        app = Veloce(openapi_url=None)
        results = []

        async def bg_work(val: str):
            results.append(val)

        @app.post("/work")
        async def do_work(request: Request, tasks: BackgroundTasks):
            tasks.add_task(bg_work, "done")
            return {"status": "queued"}

        resp = await app.handle_request(make_request(method="POST", path="/work"))
        assert resp.status_code == 200
        # Background tasks are scheduled via create_task
        import asyncio

        await asyncio.sleep(0.05)
        assert "done" in results
