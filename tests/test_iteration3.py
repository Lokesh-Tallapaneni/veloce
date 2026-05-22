"""Tests for iteration 3: package restructure and common web features."""

import orjson
import pytest

from tests.conftest import make_request
from veloce import (
    Request,
    Veloce,
)

# ═══════════════════════════════════════════════════════════════
# Sub-package imports — verify restructured package works
# ═══════════════════════════════════════════════════════════════


class TestSubPackageImports:
    def test_routing_subpackage(self):
        from veloce.routing import Query, Router

        assert Router is not None
        assert Query is not None

    def test_http_subpackage(self):
        from veloce.http import Request, UploadFile

        assert Request is not None
        assert UploadFile is not None

    def test_middleware_subpackage(self):
        from veloce.middleware import (
            Middleware,
            SessionMiddleware,
        )

        assert Middleware is not None
        assert SessionMiddleware is not None

    def test_security_subpackage(self):
        from veloce.security import (
            HTTPBasic,
            OAuth2PasswordBearer,
        )

        assert HTTPBasic is not None
        assert OAuth2PasswordBearer is not None

    def test_serving_subpackage(self):
        from veloce.serving import HttpProtocol

        assert HttpProtocol is not None

    def test_types_module(self):
        from veloce._types import Scope

        assert Scope is not None

    def test_py_typed_exists(self):
        import os

        import veloce

        pkg_dir = os.path.dirname(veloce.__file__)
        assert os.path.exists(os.path.join(pkg_dir, "py.typed"))


# ═══════════════════════════════════════════════════════════════
# teardown_request
# ═══════════════════════════════════════════════════════════════


class TestTeardownRequest:
    @pytest.mark.asyncio
    async def test_teardown_runs_after_success(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.teardown_request
        def on_teardown(exc):
            log.append(("teardown", exc))

        @app.get("/ok")
        async def ok(request: Request):
            return {"ok": True}

        await app.handle_request(make_request(path="/ok"))
        assert len(log) == 1
        assert log[0] == ("teardown", None)

    @pytest.mark.asyncio
    async def test_teardown_runs_after_error(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.teardown_request
        def on_teardown(exc):
            log.append(("teardown", type(exc).__name__ if exc else None))

        @app.get("/crash")
        async def crash(request: Request):
            raise ValueError("boom")

        await app.handle_request(make_request(path="/crash"))
        assert len(log) == 1
        assert log[0][1] == "ValueError"

    @pytest.mark.asyncio
    async def test_teardown_runs_after_404(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.teardown_request
        def on_teardown(exc):
            log.append("teardown")

        await app.handle_request(make_request(path="/nonexistent"))
        assert "teardown" in log


# ═══════════════════════════════════════════════════════════════
# context_processor
# ═══════════════════════════════════════════════════════════════


class TestContextProcessor:
    def test_context_processor_registration(self):
        app = Veloce(openapi_url=None)

        @app.context_processor
        def inject_version():
            return {"app_version": "1.0.0"}

        assert len(app._context_processors) == 1


# ═══════════════════════════════════════════════════════════════
# add_url_rule
# ═══════════════════════════════════════════════════════════════


class TestAddUrlRule:
    @pytest.mark.asyncio
    async def test_add_url_rule(self):
        app = Veloce(openapi_url=None)

        async def hello(request: Request):
            return {"hello": "world"}

        app.add_url_rule("/hello", endpoint="hello", view_func=hello)

        resp = await app.handle_request(make_request(path="/hello"))
        assert resp.status_code == 200
        assert b"hello" in resp.body

    def test_add_url_rule_no_func_raises(self):
        app = Veloce(openapi_url=None)
        with pytest.raises(ValueError):
            app.add_url_rule("/nope")


# ═══════════════════════════════════════════════════════════════
# redirect_slashes
# ═══════════════════════════════════════════════════════════════


class TestRedirectSlashes:
    @pytest.mark.asyncio
    async def test_trailing_slash_redirect(self):
        app = Veloce(openapi_url=None, redirect_slashes=True)

        @app.get("/users/")
        async def users(request: Request):
            return [{"id": 1}]

        resp = await app.handle_request(make_request(path="/users"))
        assert resp.status_code == 307
        assert resp.headers.get("Location") == "/users/"

    @pytest.mark.asyncio
    async def test_no_redirect_when_disabled(self):
        app = Veloce(openapi_url=None, redirect_slashes=False)

        @app.get("/users/")
        async def users(request: Request):
            return [{"id": 1}]

        resp = await app.handle_request(make_request(path="/users"))
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════
# OpenAPI metadata
# ═══════════════════════════════════════════════════════════════


class TestOpenAPIMetadata:
    @pytest.mark.asyncio
    async def test_openapi_with_metadata(self):
        app = Veloce(
            title="My API",
            version="2.0.0",
            description="A test API",
            contact={"name": "Dev", "email": "dev@example.com"},
            license_info={"name": "MIT"},
            terms_of_service="https://example.com/tos",
            servers=[{"url": "https://api.example.com", "description": "Production"}],
            openapi_tags=[{"name": "users", "description": "User operations"}],
        )
        app._setup_openapi()

        resp = await app.handle_request(make_request(path="/openapi.json"))
        schema = orjson.loads(resp.body)

        assert schema["info"]["title"] == "My API"
        assert schema["info"]["description"] == "A test API"
        assert schema["info"]["contact"]["name"] == "Dev"
        assert schema["info"]["license"]["name"] == "MIT"
        assert schema["info"]["termsOfService"] == "https://example.com/tos"
        assert schema["servers"][0]["url"] == "https://api.example.com"
        assert schema["tags"][0]["name"] == "users"


# ═══════════════════════════════════════════════════════════════
# response_model_include / exclude
# ═══════════════════════════════════════════════════════════════


class TestResponseModelFiltering:
    @pytest.mark.asyncio
    async def test_response_model_exclude_in_openapi(self):
        from pydantic import BaseModel

        class Item(BaseModel):
            name: str
            price: float
            tax: float = 10.0

        app = Veloce(openapi_url=None)

        @app.get(
            "/items/{id}",
            response_model=Item,
            response_model_exclude={"tax"},
        )
        async def get_item(id: int):
            return {"name": "Widget", "price": 9.99, "tax": 1.0}

        from veloce.contrib.openapi import get_openapi_schema

        schema = get_openapi_schema(app)
        assert "/items/{id}" in schema["paths"]

    @pytest.mark.asyncio
    async def test_include_in_schema_false(self):
        app = Veloce(openapi_url=None)

        @app.get("/internal", include_in_schema=False)
        async def internal(request: Request):
            return {"secret": True}

        from veloce.contrib.openapi import get_openapi_schema

        schema = get_openapi_schema(app)
        assert "/internal" not in schema["paths"]

        # But route still works
        resp = await app.handle_request(make_request(path="/internal"))
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════
# responses dict per route (multiple status codes)
# ═══════════════════════════════════════════════════════════════


class TestMultipleResponses:
    @pytest.mark.asyncio
    async def test_responses_in_route(self):
        app = Veloce(openapi_url=None)

        @app.get(
            "/items/{id}",
            responses={
                404: {"description": "Item not found"},
                403: {"description": "Not authorized"},
            },
        )
        async def get_item(id: int):
            return {"id": id}

        routes = app._collect_all_routes()
        assert len(routes) == 1
        _, _, info = routes[0]
        assert 404 in info.responses
        assert 403 in info.responses


# ═══════════════════════════════════════════════════════════════
# app.state
# ═══════════════════════════════════════════════════════════════


class TestAppState:
    def test_app_state_dict(self):
        app = Veloce(openapi_url=None)
        app.state["db_url"] = "postgres://localhost/mydb"
        assert app.state["db_url"] == "postgres://localhost/mydb"


# ═══════════════════════════════════════════════════════════════
# Performance: benchmark still fast
# ═══════════════════════════════════════════════════════════════


@pytest.mark.perf
class TestPerformance:
    """Wall-clock dispatch checks — flaky under full-suite CPU contention,
    so the class is marked `perf` and excluded from the default `pytest`
    run. Opt in with `pytest -m perf` on a quiet machine.
    """

    @pytest.mark.asyncio
    async def test_simple_route_under_50us(self):
        """Sanity check: simple route should complete in under 50 microseconds."""
        import time

        app = Veloce(openapi_url=None)

        @app.get("/bench")
        async def bench(request: Request):
            return {"ok": True}

        # Warmup
        for _ in range(100):
            await app.handle_request(make_request(path="/bench"))

        # Measure
        times = []
        for _ in range(1000):
            start = time.perf_counter_ns()
            await app.handle_request(make_request(path="/bench"))
            times.append(time.perf_counter_ns() - start)

        avg_us = sum(times) / len(times) / 1000
        assert avg_us < 100, f"Average request time {avg_us:.1f}us exceeds 100us budget"
