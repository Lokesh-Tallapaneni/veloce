"""app.include_router + the APIRouter alias."""

from __future__ import annotations

from veloce import APIRouter, Blueprint, Router, Veloce
from veloce.testclient import TestClient


def test_apirouter_is_router_alias():
    assert APIRouter is Router


def test_apirouter_constructor_shape():
    """`APIRouter(prefix=..., tags=...)` constructs without a positional name."""
    api = APIRouter(prefix="/items", tags=["items"])
    assert api.prefix == "/items"
    assert api.tags == ["items"]


def test_include_router_mounts_apirouter():
    app = Veloce()
    api = APIRouter(prefix="/api")

    @api.get("/ping")
    async def ping():
        return {"pong": True}

    app.include_router(api)
    with TestClient(app) as client:
        resp = client.get("/api/ping")
        assert resp.status_code == 200
        assert resp.json() == {"pong": True}


def test_apirouter_tags_apply_to_routes():
    app = Veloce()
    api = APIRouter(prefix="/api", tags=["items"])

    @api.get("/items")
    async def list_items():
        return []

    app.include_router(api)
    # `app.routes` is the public view and already carries `tags`; reaching
    # for `_collect_all_routes` pinned a private name this assertion says
    # nothing about.
    tagged = [route for route in app.routes if route["path"] == "/api/items"]
    assert tagged and tagged[0]["tags"] == ["items"]


def test_include_router_mounts_blueprint():
    app = Veloce()
    bp = Blueprint("api", url_prefix="/api")

    @bp.get("/ping")
    async def ping():
        return {"pong": True}

    app.include_router(bp)
    with TestClient(app) as client:
        resp = client.get("/api/ping")
        assert resp.status_code == 200
        assert resp.json() == {"pong": True}


def test_include_router_prefix_kwarg():
    """Veloce accepts the mount prefix as `prefix=`."""
    app = Veloce()
    bp = Blueprint("v2")

    @bp.get("/info")
    async def info():
        return {"v": 2}

    app.include_router(bp, prefix="/v2")
    with TestClient(app) as client:
        assert client.get("/v2/info").json() == {"v": 2}


def test_include_router_url_prefix_kwarg():
    """Veloce spells it `url_prefix=` — both accepted."""
    app = Veloce()
    bp = Blueprint("admin")

    @bp.get("/panel")
    async def panel():
        return {"area": "admin"}

    app.include_router(bp, url_prefix="/admin")
    with TestClient(app) as client:
        assert client.get("/admin/panel").json() == {"area": "admin"}


def test_include_router_registers_in_blueprints_map():
    app = Veloce()
    bp = Blueprint("reports")
    app.include_router(bp)
    assert "reports" in app.blueprints
