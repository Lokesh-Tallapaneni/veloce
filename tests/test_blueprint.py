"""Blueprint primitive — a core building block."""

from __future__ import annotations

import orjson
import pytest

from tests.conftest import make_request
from veloce import Blueprint, Request, Veloce
from veloce.testclient import TestClient


def _req(path: str, method: str = "GET") -> Request:
    return make_request(method=method, path=path, query_string="", headers={}, body=b"")


# ── Basic registration ───────────────────────────────────────────────


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

    resp1 = await app.handle_request(_req("/users/"))
    assert orjson.loads(resp1.body) == {"page": "users-index"}

    resp2 = await app.handle_request(_req("/users/42"))
    assert orjson.loads(resp2.body) == {"uid": "42"}


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
    assert orjson.loads(resp.body) == {"caught": "kaboom"}


async def test_blueprint_errorhandler_is_scoped_to_its_own_routes():
    """A blueprint's errorhandler must not catch an exception raised on a
    sibling blueprint or an app-level route - it is scoped to its own routes."""

    class ScopedError(Exception):
        pass

    bp_a = Blueprint("a", url_prefix="/a")

    @bp_a.errorhandler(ScopedError)
    async def handle(request, exc):
        return {"by": "a"}

    @bp_a.get("/boom")
    async def a_boom():
        raise ScopedError("x")

    bp_b = Blueprint("b", url_prefix="/b")

    @bp_b.get("/boom")
    async def b_boom():
        raise ScopedError("x")

    # No debug → an unhandled exception is a clean 500 JSON, not a traceback.
    app = Veloce(openapi_url=None)
    app.register_blueprint(bp_a)
    app.register_blueprint(bp_b)

    @app.get("/boom")
    async def app_boom():
        raise ScopedError("x")

    # a's handler catches a's own route.
    resp_a = await app.handle_request(_req("/a/boom"))
    assert orjson.loads(resp_a.body) == {"by": "a"}
    # a's handler does NOT catch a sibling blueprint's route.
    assert (await app.handle_request(_req("/b/boom"))).status_code == 500
    # a's handler does NOT catch an app-level route.
    assert (await app.handle_request(_req("/boom"))).status_code == 500


async def test_blueprint_status_handler_catches_unhandled_exception():
    """A blueprint `@bp.errorhandler(500)` fires for a plain unhandled exception
    on its own route (the unhandled-exception -> 500 path is scoped too)."""
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.errorhandler(500)
    async def handle_500(request):
        return {"by": "bp500"}

    @bp.get("/boom")
    async def boom():
        raise RuntimeError("x")

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    @app.get("/boom")
    async def app_boom():
        raise RuntimeError("x")

    resp = await app.handle_request(_req("/bp/boom"))
    assert orjson.loads(resp.body) == {"by": "bp500"}
    # An app-level route's unhandled error is not caught by the blueprint handler.
    assert orjson.loads((await app.handle_request(_req("/boom"))).body) == {
        "detail": "Internal Server Error"
    }


async def test_nested_sibling_blueprints_scope_handlers():
    """Two sibling child blueprints under the same parent keep their own
    handlers - a shared exception type does not leak from one child to the other."""

    class E(Exception):
        pass

    parent = Blueprint("p", url_prefix="/p")
    c1 = Blueprint("c1", url_prefix="/c1")
    c2 = Blueprint("c2", url_prefix="/c2")

    @c1.errorhandler(E)
    async def h1(request, exc):
        return {"by": "c1"}

    @c2.errorhandler(E)
    async def h2(request, exc):
        return {"by": "c2"}

    @c1.get("/boom")
    async def b1():
        raise E()

    @c2.get("/boom")
    async def b2():
        raise E()

    parent.register_blueprint(c1)
    parent.register_blueprint(c2)
    app = Veloce(openapi_url=None)
    app.register_blueprint(parent)

    assert orjson.loads((await app.handle_request(_req("/p/c1/boom"))).body) == {"by": "c1"}
    assert orjson.loads((await app.handle_request(_req("/p/c2/boom"))).body) == {"by": "c2"}


def test_error_handler_spec_reports_per_blueprint_subtables():
    bp = Blueprint("shop", url_prefix="/shop")

    class ShopError(Exception):
        pass

    @bp.errorhandler(ShopError)
    async def handle(request, exc):
        return {}

    @bp.errorhandler(404)
    async def not_found(request):
        return {}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    spec = app.error_handler_spec
    assert ShopError in spec["shop"]
    assert 404 in spec["shop"]
    # App-level handlers stay under the None key.
    assert None in spec


# ── Mountable on multiple apps / multiple times ──────────────────────


async def test_same_blueprint_mounted_on_two_apps():
    bp = Blueprint("hello", url_prefix="/h")

    @bp.get("/")
    async def hi():
        return {"x": 1}

    app_a = Veloce(debug=True, openapi_url=None)
    app_a.register_blueprint(bp)
    app_b = Veloce(debug=True, openapi_url=None)
    app_b.register_blueprint(bp, url_prefix="/world")

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


def test_blueprint_bare_prefix_route_serves_bare_url():
    """`@bp.get("")` against a prefixed blueprint must serve the bare
    URL (`/x`), not redirect to `/x/`. The previous `_walk_routes`
    coerced an empty stripped path to `/`, which made the app
    register `/x/` and 308-redirect / 404 the bare URL."""
    bp = Blueprint("x", url_prefix="/x")

    @bp.get("")
    def root_get():
        return {"hit": "bare-prefix"}

    @bp.post("")
    def root_post():
        return {"posted": True}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    client = TestClient(app)
    # Bare URL — no trailing slash — must hit the handler directly.
    resp = client.get("/x")
    assert resp.status_code == 200
    assert resp.json() == {"hit": "bare-prefix"}

    # POST /x must also reach the handler without a 308 detour.
    resp_post = client.post("/x")
    assert resp_post.status_code == 200
    assert resp_post.json() == {"posted": True}


def test_blueprint_root_slash_route_still_serves_trailing_slash():
    """The trailing-slash route shape `@bp.get("/")` must keep working
    — distinct from `@bp.get("")` after the fix."""
    bp = Blueprint("y", url_prefix="/y")

    @bp.get("/")
    def root_slash():
        return {"hit": "trailing-slash"}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    resp = TestClient(app).get("/y/")
    assert resp.status_code == 200
    assert resp.json() == {"hit": "trailing-slash"}
