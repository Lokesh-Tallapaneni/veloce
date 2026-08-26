"""R5 — router-level `dependencies=` apply to every route."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Blueprint, Depends, Request, Router, Veloce


def _req(path: str = "/x") -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


@pytest.mark.asyncio
async def test_router_level_dependency_fires_for_each_route():
    """A `dependencies=[Depends(...)]` on the Router runs for every route."""
    calls: list[str] = []

    async def gate():
        calls.append("gate")
        return None

    bp = Blueprint("api", url_prefix="/api", dependencies=[Depends(gate)])

    @bp.get("/a")
    async def a():
        return {"r": "a"}

    @bp.get("/b")
    async def b():
        return {"r": "b"}

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(bp)

    await app.handle_request(_req("/api/a"))
    await app.handle_request(_req("/api/b"))
    assert calls == ["gate", "gate"]


@pytest.mark.asyncio
async def test_router_and_route_dependencies_both_run():
    """Per-route deps append after router-level deps."""
    order: list[str] = []

    async def outer():
        order.append("outer")

    async def inner():
        order.append("inner")

    bp = Blueprint("api", url_prefix="/api", dependencies=[Depends(outer)])

    @bp.get("/x", dependencies=[Depends(inner)])
    async def x():
        return {}

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(bp)

    await app.handle_request(_req("/api/x"))
    assert order == ["outer", "inner"]


@pytest.mark.asyncio
async def test_router_dependency_value_available_via_depends():
    """Router-level dep's return value still flows through DI for handlers
    that ask for it explicitly with `Depends`."""

    async def get_db():
        return {"db": True}

    bp = Blueprint("api", url_prefix="/api", dependencies=[Depends(get_db)])

    @bp.get("/x")
    async def x(db=Depends(get_db)):
        return {"connected": db["db"]}

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(bp)

    import orjson

    resp = await app.handle_request(_req("/api/x"))
    assert orjson.loads(resp.body) == {"connected": True}


def test_router_init_accepts_dependencies():
    """A plain Router (no Blueprint) accepts the same kwarg."""
    r = Router(prefix="/v1", dependencies=[Depends(lambda: None)])
    assert len(r.router_dependencies) == 1
