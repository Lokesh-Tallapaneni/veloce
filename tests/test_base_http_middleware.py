"""BaseHTTPMiddleware tests (M9)."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import BaseHTTPMiddleware, Request, Veloce


def _req(path: str = "/x") -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── Subclass-based usage ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subclass_dispatch_runs_around_handler():
    events: list[str] = []

    class TraceMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            events.append("before")
            response = await call_next(request)
            events.append("after")
            return response

    app = Veloce(debug=True, openapi_url=None)
    app.add_http_middleware(TraceMW())

    @app.get("/x")
    async def x():
        events.append("handler")
        return {"ok": True}

    resp = await app.handle_request(_req())
    assert resp.status_code == 200
    assert events == ["before", "handler", "after"]


@pytest.mark.asyncio
async def test_subclass_can_mutate_response_headers():
    class HeaderMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            response.headers["X-MW"] = "yes"
            return response

    app = Veloce(debug=True, openapi_url=None)
    app.add_http_middleware(HeaderMW())

    @app.get("/x")
    async def x():
        return {"ok": True}

    resp = await app.handle_request(_req())
    assert resp.headers.get("X-MW") == "yes"


# ── Functional usage via dispatch= ───────────────────────────────────


@pytest.mark.asyncio
async def test_functional_dispatch_via_init_kwarg():
    """`BaseHTTPMiddleware(dispatch=fn)` binds a bare function as dispatch."""

    async def fn(request, call_next):
        response = await call_next(request)
        response.headers["X-Functional"] = "1"
        return response

    app = Veloce(debug=True, openapi_url=None)
    app.add_http_middleware(BaseHTTPMiddleware(dispatch=fn))

    @app.get("/x")
    async def x():
        return {}

    resp = await app.handle_request(_req())
    assert resp.headers.get("X-Functional") == "1"


# ── Class registration (auto-instantiation) ──────────────────────────


@pytest.mark.asyncio
async def test_add_http_middleware_accepts_a_class():
    """Passing a class to `add_http_middleware` instantiates it with no args."""

    class MW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            response.headers["X-Class"] = "v"
            return response

    app = Veloce(debug=True, openapi_url=None)
    app.add_http_middleware(MW)

    @app.get("/x")
    async def x():
        return {}

    resp = await app.handle_request(_req())
    assert resp.headers.get("X-Class") == "v"


def test_add_http_middleware_rejects_non_callable():
    app = Veloce(debug=True, openapi_url=None)
    with pytest.raises(TypeError):
        app.add_http_middleware(42)  # type: ignore[arg-type]


# ── Composition: multiple middlewares chain ──────────────────────────


@pytest.mark.asyncio
async def test_multiple_class_middlewares_compose_in_registration_order():
    events: list[str] = []

    class A(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            events.append("A-in")
            response = await call_next(request)
            events.append("A-out")
            return response

    class B(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            events.append("B-in")
            response = await call_next(request)
            events.append("B-out")
            return response

    app = Veloce(debug=True, openapi_url=None)
    app.add_http_middleware(A())
    app.add_http_middleware(B())

    @app.get("/x")
    async def x():
        events.append("H")
        return {}

    await app.handle_request(_req())
    # A outermost, B inner, then handler — onion shape.
    assert events == ["A-in", "B-in", "H", "B-out", "A-out"]


@pytest.mark.asyncio
async def test_class_middleware_composes_with_decorator_middleware():
    """`add_http_middleware` and `@app.middleware("http")` share the same chain."""
    events: list[str] = []

    class ClassMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            events.append("class-in")
            r = await call_next(request)
            events.append("class-out")
            return r

    app = Veloce(debug=True, openapi_url=None)
    app.add_http_middleware(ClassMW())

    @app.middleware("http")
    async def fn_mw(request, call_next):
        events.append("fn-in")
        r = await call_next(request)
        events.append("fn-out")
        return r

    @app.get("/x")
    async def x():
        events.append("H")
        return {}

    await app.handle_request(_req())
    # Class registered first → outermost; fn registered next → inner.
    assert events == ["class-in", "fn-in", "H", "fn-out", "class-out"]


# ── Sanity ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_dispatch_is_passthrough():
    """A bare `BaseHTTPMiddleware()` (no override) just calls through."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_http_middleware(BaseHTTPMiddleware())

    @app.get("/x")
    async def x():
        return {"ok": True}

    resp = await app.handle_request(_req())
    assert resp.status_code == 200
    assert b'"ok":true' in resp.body
