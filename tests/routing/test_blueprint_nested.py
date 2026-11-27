"""Nested Blueprint support."""

from __future__ import annotations

import orjson
import pytest

from tests.conftest import make_request
from veloce import Blueprint, Request, Veloce


def _req(path: str) -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


async def test_nested_blueprint_combines_prefixes():
    api = Blueprint("api", url_prefix="/api")
    users = Blueprint("users", url_prefix="/users")

    @users.get("/{uid}")
    async def detail(uid: str):
        return {"uid": uid}

    api.register_blueprint(users)

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(api)

    resp = await app.handle_request(_req("/api/users/42"))
    assert orjson.loads(resp.body) == {"uid": "42"}


async def test_nested_prefix_override():
    """url_prefix passed to register_blueprint(child, ...) takes precedence."""
    parent = Blueprint("p", url_prefix="/p")
    child = Blueprint("c", url_prefix="/c")

    @child.get("/hi")
    async def hi():
        return {"ok": True}

    parent.register_blueprint(child, url_prefix="/x")

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(parent)

    # Mounted at /p + /x + /hi.
    r = await app.handle_request(_req("/p/x/hi"))
    assert r.status_code == 200
    # /p/c/hi is NOT mounted.
    r2 = await app.handle_request(_req("/p/c/hi"))
    assert r2.status_code == 404


async def test_child_hooks_fire_under_parent_gate():
    """A hook on the child blueprint runs for the nested path."""
    parent = Blueprint("p", url_prefix="/p")
    child = Blueprint("c", url_prefix="/c")
    seen: list[str] = []

    @child.before_request
    def trace(request):
        seen.append(request.path)

    @child.get("/x")
    async def x():
        return {}

    parent.register_blueprint(child)

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(parent)

    await app.handle_request(_req("/p/c/x"))
    assert seen == ["/p/c/x"]


async def test_child_errorhandler_inherited():
    """An errorhandler on the child catches exceptions on nested routes."""
    parent = Blueprint("p", url_prefix="/p")
    child = Blueprint("c", url_prefix="/c")

    class BPError(Exception):
        pass

    @child.errorhandler(BPError)
    async def handle(request, exc):
        return {"caught": str(exc)}

    @child.get("/boom")
    async def boom():
        raise BPError("kaboom")

    parent.register_blueprint(child)

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(parent)

    resp = await app.handle_request(_req("/p/c/boom"))
    assert orjson.loads(resp.body) == {"caught": "kaboom"}


def test_register_blueprint_rejects_self_registration():
    bp = Blueprint("self_loop", url_prefix="/x")

    with pytest.raises(ValueError, match="itself"):
        bp.register_blueprint(bp)
