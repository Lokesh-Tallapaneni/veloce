"""app.include_router + APIRouter alias."""

from __future__ import annotations

from veloce import APIRouter, Blueprint, Veloce
from veloce.testclient import TestClient


def test_apirouter_is_blueprint_alias():
    assert APIRouter is Blueprint


def test_include_router_mounts_blueprint():
    app = Veloce()
    api = APIRouter("api", url_prefix="/api")

    @api.get("/ping")
    async def ping():
        return {"pong": True}

    app.include_router(api)
    with TestClient(app) as client:
        resp = client.get("/api/ping")
        assert resp.status_code == 200
        assert resp.json() == {"pong": True}


def test_include_router_prefix_kwarg():
    """Veloce spells the mount prefix `prefix=`."""
    app = Veloce()
    api = APIRouter("v2")

    @api.get("/info")
    async def info():
        return {"v": 2}

    app.include_router(api, prefix="/v2")
    with TestClient(app) as client:
        assert client.get("/v2/info").json() == {"v": 2}


def test_include_router_url_prefix_kwarg():
    """Veloce spells it `url_prefix=` — both accepted."""
    app = Veloce()
    api = APIRouter("admin")

    @api.get("/panel")
    async def panel():
        return {"area": "admin"}

    app.include_router(api, url_prefix="/admin")
    with TestClient(app) as client:
        assert client.get("/admin/panel").json() == {"area": "admin"}


def test_include_router_registers_in_blueprints_map():
    app = Veloce()
    api = APIRouter("reports")
    app.include_router(api)
    assert "reports" in app.blueprints
