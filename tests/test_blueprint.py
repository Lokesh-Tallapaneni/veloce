"""Blueprint primitive — a core building block."""

from __future__ import annotations

import pytest

from veloce import Blueprint, Request, Veloce


def _req(path: str, method: str = "GET") -> Request:
    return Request(method=method, path=path, query_string="", headers={}, body=b"")


# ── Basic registration ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blueprint_routes_mount_under_prefix():
    bp = Blueprint("users", url_prefix="/users")

    @bp.get("/")
    async def index():
        return {"page": "users-index"}

    @bp.get("/{uid}")
    async def detail(uid: str):
        return {"uid": uid}

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(bp)

    import orjson

    resp1 = await app.handle_request(_req("/users/"))
    assert orjson.loads(resp1.body) == {"page": "users-index"}

    resp2 = await app.handle_request(_req("/users/42"))
    assert orjson.loads(resp2.body) == {"uid": "42"}


@pytest.mark.asyncio
async def test_register_blueprint_with_prefix_override():
    """url_prefix passed to register_blueprint takes precedence."""
    bp = Blueprint("api", url_prefix="/v1")

    @bp.get("/ping")
    async def ping():
        return {"ok": True}

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(bp, url_prefix="/v2")

    resp = await app.handle_request(_req("/v2/ping"))
    assert resp.status_code == 200
    # Original /v1/ping not mounted.
    resp_miss = await app.handle_request(_req("/v1/ping"))
    assert resp_miss.status_code == 404


# ── before_request / after_request gate on blueprint endpoints ───────


@pytest.mark.asyncio
async def test_before_request_only_fires_for_blueprint_routes():
    bp = Blueprint("auth", url_prefix="/auth")
    seen: list[str] = []

    @bp.before_request
    def trace(request):
        seen.append(request.path)

    @bp.get("/login")
    async def login():
        return {}

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/other")
    async def other():
        return {}

    app.register_blueprint(bp)

    await app.handle_request(_req("/auth/login"))
    await app.handle_request(_req("/other"))
    assert seen == ["/auth/login"]


@pytest.mark.asyncio
async def test_after_request_runs_for_blueprint_endpoint():
    bp = Blueprint("bp1", url_prefix="/bp1")

    @bp.after_request
    def add_header(request, response):
        response.headers["X-BP"] = "yes"

    @bp.get("/x")
    async def x():
        return {}

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(bp)

    resp = await app.handle_request(_req("/bp1/x"))
    assert resp.headers["X-BP"] == "yes"


# ── Blueprint errorhandler ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_blueprint_errorhandler_catches_blueprint_routes():
    bp = Blueprint("api", url_prefix="/api")

    class BPError(Exception):
        pass

    @bp.errorhandler(BPError)
    async def handle(request, exc):
        return {"caught": str(exc)}

    @bp.get("/boom")
    async def boom():
        raise BPError("kaboom")

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(bp)

    resp = await app.handle_request(_req("/api/boom"))
    import orjson

    assert orjson.loads(resp.body) == {"caught": "kaboom"}


# ── Mountable on multiple apps / multiple times ──────────────────────


@pytest.mark.asyncio
async def test_same_blueprint_mounted_on_two_apps():
    bp = Blueprint("hello", url_prefix="/h")

    @bp.get("/")
    async def hi():
        return {"x": 1}

    app_a = Veloce(debug=True, openapi_url=None)
    app_a.register_blueprint(bp)
    app_b = Veloce(debug=True, openapi_url=None)
    app_b.register_blueprint(bp, url_prefix="/world")

    import orjson

    ra = await app_a.handle_request(_req("/h/"))
    assert orjson.loads(ra.body) == {"x": 1}

    rb = await app_b.handle_request(_req("/world/"))
    assert orjson.loads(rb.body) == {"x": 1}


def test_register_blueprint_type_check():
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="expects a Blueprint"):
        app.register_blueprint("not a blueprint")  # type: ignore[arg-type]


def test_blueprint_hidden_route_is_reachable():
    """include_in_schema=False hides a route from OpenAPI; it stays reachable."""
    bp = Blueprint("demo", url_prefix="")

    @bp.get("/hidden", include_in_schema=False)
    async def hidden():
        return {"ok": True}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    assert app.test_client().get("/hidden").status_code == 200


def test_blueprint_websocket_route_is_registered():
    """A blueprint's WebSocket route enters the app's radix tree."""
    bp = Blueprint("ws_demo", url_prefix="")

    @bp.websocket("/bws")
    async def handler(ws):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    with app.test_client().websocket_connect("/bws") as ws:
        assert ws.receive_text() == "hi"
