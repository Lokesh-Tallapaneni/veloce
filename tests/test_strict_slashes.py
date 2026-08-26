"""Per-route `strict_slashes=False` override."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce


def _req(path: str) -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── Default (strict) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_redirects_when_slash_mismatches():
    """Default behaviour — `/x/` redirects to `/x` (or vice versa)."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {}

    resp = await app.handle_request(_req("/x/"))
    # Without strict_slashes=False, a slashed request gets a redirect.
    assert resp.status_code in (307, 308)


# ── strict_slashes=False ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_strict_slashes_false_matches_both_forms():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", strict_slashes=False)
    async def x():
        return {"ok": True}

    # Both forms reach the handler — no redirect.
    resp1 = await app.handle_request(_req("/x"))
    resp2 = await app.handle_request(_req("/x/"))
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    import orjson

    assert orjson.loads(resp1.body) == {"ok": True}
    assert orjson.loads(resp2.body) == {"ok": True}


@pytest.mark.asyncio
async def test_strict_slashes_false_with_trailing_form_too():
    """Registering with the slashed form + strict_slashes=False also
    accepts the unslashed form."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/items/", strict_slashes=False)
    async def items():
        return {"items": []}

    resp1 = await app.handle_request(_req("/items"))
    resp2 = await app.handle_request(_req("/items/"))
    assert resp1.status_code == 200
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_registering_slashed_sibling_keeps_unslashed_match():
    """Registering `/users/` must not flip the already-registered `/users`
    route to a slash redirect — the two share one radix node."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/users")
    async def list_users():
        return {"slashed": False}

    @app.post("/users/")
    async def create_user():
        return {"slashed": True}

    # `GET /users` must still resolve to its handler, not redirect to `/users/`.
    resp = await app.handle_request(_req("/users"))
    assert resp.status_code == 200
    import orjson

    assert orjson.loads(resp.body) == {"slashed": False}

    # The slashed form still resolves to its own handler.
    post = Request(method="POST", path="/users/", query_string="", headers={}, body=b"")
    resp_post = await app.handle_request(post)
    assert resp_post.status_code == 200
    assert orjson.loads(resp_post.body) == {"slashed": True}


@pytest.mark.asyncio
async def test_strict_slashes_only_affects_decorated_route():
    """Other routes still follow the global redirect_slashes policy."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/relaxed", strict_slashes=False)
    async def relaxed():
        return {}

    @app.get("/strict")
    async def strict():
        return {}

    # /relaxed/ matches without redirect.
    r1 = await app.handle_request(_req("/relaxed/"))
    assert r1.status_code == 200
    # /strict/ redirects.
    r2 = await app.handle_request(_req("/strict/"))
    assert r2.status_code in (307, 308)


# ── The declaration survives every registration path ─────────────────
#
# `strict_slashes` shapes the radix node and the regex route rather than the
# request, so it lived only on those - and `_readd_route`, which rebuilds a
# route from its `RouteInfo`, had nothing to read. A blueprint route declared
# `strict_slashes=False` therefore lost it on registration, while the same route
# reached through `include_router` kept it: one declaration, two behaviours,
# decided by how the route happened to be composed.
#
# `_readd_route`'s own docstring promised that a `RouteInfo` field is forwarded
# on every re-registration path. It is a field now, so the promise holds.


def _tolerant_blueprint():
    from veloce import Blueprint

    bp = Blueprint("bp", url_prefix="/bp")

    @bp.get("/x", strict_slashes=False)
    async def view():
        return {"ok": True}

    return bp


def _tolerant_router():
    from veloce.routing.router import Router

    sub = Router(prefix="/sub")

    @sub.get("/x", strict_slashes=False)
    async def view():
        return {"ok": True}

    return sub


def _client(app: Veloce):
    from veloce.testclient import TestClient

    return TestClient(app)


def test_a_blueprint_route_keeps_strict_slashes_false():
    """The defect: the blueprint path dropped it and the slashed form 404'd."""
    app = Veloce(redirect_slashes=False, openapi_url=None)
    app.register_blueprint(_tolerant_blueprint())
    client = _client(app)
    assert client.get("/bp/x").status_code == 200
    assert client.get("/bp/x/").status_code == 200


def test_an_included_router_route_keeps_strict_slashes_false():
    app = Veloce(redirect_slashes=False, openapi_url=None)
    app.include_router(_tolerant_router())
    client = _client(app)
    assert client.get("/sub/x").status_code == 200
    assert client.get("/sub/x/").status_code == 200


def test_both_registration_paths_agree():
    """One declaration, one behaviour, however the route was composed."""
    blueprint_app = Veloce(redirect_slashes=False, openapi_url=None)
    blueprint_app.register_blueprint(_tolerant_blueprint())
    router_app = Veloce(redirect_slashes=False, openapi_url=None)
    router_app.include_router(_tolerant_router())
    assert (
        _client(blueprint_app).get("/bp/x/").status_code
        == _client(router_app).get("/sub/x/").status_code
    )


def test_a_nested_blueprint_route_keeps_it_too():
    from veloce import Blueprint

    parent = Blueprint("parent", url_prefix="/p")
    parent.register_blueprint(_tolerant_blueprint())
    app = Veloce(redirect_slashes=False, openapi_url=None)
    app.register_blueprint(parent)
    assert _client(app).get("/p/bp/x/").status_code == 200


def test_a_blueprint_route_without_the_override_stays_strict():
    """Carrying the flag must not make every blueprint route tolerant."""
    from veloce import Blueprint

    bp = Blueprint("strict", url_prefix="/s")

    @bp.get("/x")
    async def view():
        return {"ok": True}

    app = Veloce(redirect_slashes=False, openapi_url=None)
    app.register_blueprint(bp)
    client = _client(app)
    assert client.get("/s/x").status_code == 200
    assert client.get("/s/x/").status_code == 404
