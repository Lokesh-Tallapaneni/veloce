"""Request.url_rule / .blueprint / .blueprints."""

from __future__ import annotations

from veloce import Request, Veloce
from veloce.blueprints import Blueprint
from veloce.testclient import TestClient


def _req() -> Request:
    return Request(method="GET", path="/", query_string="", headers={}, body=b"")


# ── synthetic Request defaults ───────────────────────────────────────


def test_url_rule_none_when_unmatched():
    assert _req().url_rule is None


def test_blueprint_none_when_endpoint_unset():
    assert _req().blueprint is None


def test_blueprints_empty_when_endpoint_unset():
    assert _req().blueprints == []


def test_blueprint_none_for_top_level_endpoint():
    r = _req()
    r.endpoint = "index"
    assert r.blueprint is None
    assert r.blueprints == []


def test_blueprint_extracts_prefix_for_dotted_endpoint():
    r = _req()
    r.endpoint = "api.users.list"
    assert r.blueprint == "api.users"
    assert r.blueprints == ["api.users", "api"]


# ── populated by dispatcher ──────────────────────────────────────────


def test_url_rule_populated_after_match():
    app = Veloce()
    captured: dict[str, str | None] = {}

    @app.get("/items/{id:int}")
    async def get_item(request: Request, id: int):
        captured["url_rule"] = request.url_rule
        captured["endpoint"] = request.endpoint
        return {}

    with TestClient(app) as client:
        resp = client.get("/items/7")
        assert resp.status_code == 200
    assert captured["url_rule"] == "/items/{id:int}"
    assert captured["endpoint"] == "get_item"


def test_blueprint_set_for_routes_registered_via_blueprint():
    app = Veloce()
    bp = Blueprint("api", url_prefix="/api")
    captured: dict[str, str | None] = {}

    @bp.get("/x")
    async def x(request: Request):
        captured["blueprint"] = request.blueprint
        captured["endpoint"] = request.endpoint
        return {}

    app.register_blueprint(bp)
    with TestClient(app) as client:
        client.get("/api/x")

    assert captured["endpoint"] == "api.x"
    assert captured["blueprint"] == "api"
