"""A request path containing an empty segment must not match a route.

`_cached_split_path` drops empty segments, so `//admin/users` produced the same
segment tuple as `/admin/users` and the radix tree dispatched it to that
handler - while `request.path` still read `//admin/users`. Any prefix check
written against the path (an admin gate in `before_request`, tenant scoping, an
upstream ACL) therefore saw a different string than the router matched, and one
extra slash stepped around it.

The static map is keyed by the exact path, so it never matched these; only the
tree collapsed them.
"""

from __future__ import annotations

from veloce import JSONResponse, Veloce
from veloce.testclient import TestClient


def test_a_leading_double_slash_does_not_reach_the_handler():
    """NEGATIVE: the prefix gate and the router must see the same path."""
    app = Veloce()

    @app.before_request
    async def gate(request):
        if request.path.startswith("/admin"):
            return JSONResponse({"detail": "forbidden"}, status_code=403)

    @app.get("/admin/users")
    async def admin_users(request):
        return {"users": ["root"]}

    with TestClient(app) as client:
        assert client.get("/admin/users").status_code == 403
        assert client.get("//admin/users").status_code == 404


def test_an_interior_double_slash_does_not_match():
    """NEGATIVE: the same collapse applies inside the path."""
    app = Veloce()

    @app.get("/a/b")
    async def ab(request):
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/a/b").status_code == 200
        assert client.get("/a//b").status_code == 404


def test_a_trailing_slash_still_behaves_as_before():
    """POSITIVE: `/a/` has a trailing empty segment but no `//`.

    The trailing-slash rule lives in `_match_tree`; refusing empty segments
    must not take it over.
    """
    app = Veloce()

    @app.get("/a")
    async def a(request):
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/a").status_code == 200
        assert client.get("/a/", follow_redirects=False).status_code in (200, 307, 308)


def test_the_root_path_still_matches():
    """POSITIVE: `/` must not be caught by an empty-segment rule."""
    app = Veloce()

    @app.get("/")
    async def root(request):
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/").status_code == 200


def test_a_parameterised_route_still_matches():
    """POSITIVE: the guard sits on the path dynamic routes take."""
    app = Veloce()

    @app.get("/items/{item_id}")
    async def item(request, item_id: str):
        return {"id": item_id}

    with TestClient(app) as client:
        assert client.get("/items/42").json() == {"id": "42"}


# ── the same rules at the router level, without a client ─────────────
#
# `match` and `get_allowed_methods` are written separately and can disagree;
# when they did, a refused path still reported its methods and the 404 became a
# 405 that confirmed the route exists. These assert the pair together, and run
# without a socket so they survive an environment where `TestClient` cannot.


def _router_with_routes() -> Veloce:
    app = Veloce()

    @app.get("/")
    async def root(request):
        return {}

    @app.get("/a")
    async def a(request):
        return {}

    @app.get("/a/b")
    async def ab(request):
        return {}

    return app


def test_a_double_slash_path_neither_matches_nor_reports_methods():
    """NEGATIVE: refusing to match must not leave `Allow` advertising the route."""
    app = _router_with_routes()

    for path in ("//a", "/a//b", "//a/b"):
        assert app.match("GET", path) is None, path
        assert app.get_allowed_methods(path) == [], path


def test_canonical_paths_still_match_and_report_methods():
    """POSITIVE: only the empty-segment form changes; the rest is untouched."""
    app = _router_with_routes()

    for path in ("/", "/a", "/a/b"):
        assert app.match("GET", path) is not None, path
        assert app.get_allowed_methods(path) == ["GET"], path
