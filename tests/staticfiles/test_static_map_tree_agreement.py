"""The static-map fast path and the radix tree resolve a path identically.

`Router.match` consults an O(1) map of literal paths before walking the tree.
Which nodes are eligible for that map is decided by one predicate in
`_build_static_routes`:

    node.handlers and not node.trailing_slash and not node.tolerant_slash

and whether a walked match survives is decided by a *different*, four-flag
predicate ~50 lines later in `_match_tree`:

    if not result.tolerant_slash:
        if result.trailing_slash and not result.unslashed_variant and not request_has_slash: ...
        if result.unslashed_variant and not result.trailing_slash and request_has_slash: ...

The two are written in different shapes and must stay consistent: the map's job
is to exclude exactly the nodes whose gate could fire. Nothing tied them
together, so a change to the gate that was not mirrored into the exclusion would
make the fast path serve a request the slow path rejects - a routing difference
that appears only once a route is hot enough to be in the map, which is to say
in production and not in a unit test.

These tests state the invariant directly, over the product of every slash shape
a route can be registered in and every shape a request can arrive in.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.routing.router import Router

METHODS = ["GET", "POST"]

REQUEST_PATHS = ["/a", "/a/", "/", "/a/b", "/a/b/", "/missing", "/missing/"]


def _router(*, routes: tuple[tuple[str, str], ...], strict_slashes: bool | None = None) -> Router:
    router = Router()
    for path, method in routes:

        async def handler():
            return {}

        router.add_route(path, handler, methods=[method], strict_slashes=strict_slashes)
    return router


# `/a` and `/a/` share a tree node and collide per method, so the shape where
# *both* slash flags are set on one node is registered under two methods - which
# is the only way an application can reach it too.
SHAPES = {
    "unslashed": {"routes": (("/a", "GET"),)},
    "slashed": {"routes": (("/a/", "GET"),)},
    "both": {"routes": (("/a", "GET"), ("/a/", "POST"))},
    "tolerant": {"routes": (("/a", "GET"),), "strict_slashes": False},
    "tolerant-slashed": {"routes": (("/a/", "GET"),), "strict_slashes": False},
    "nested": {"routes": (("/a/b", "GET"),)},
    "nested-slashed": {"routes": (("/a/b/", "GET"),)},
    "root": {"routes": (("/", "GET"),)},
}


def _tree_info(router: Router, method: str, path: str):
    match = router._match_tree(method, path)
    return None if match is None else match.route_info


def _map_info(router: Router, method: str, path: str):
    smap = router._static_routes
    if smap is None:
        smap = router._static_routes = router._build_static_routes()
    return smap.get((method, path))


# ── the invariant ────────────────────────────────────────────────────


@pytest.mark.parametrize("shape", list(SHAPES))
@pytest.mark.parametrize("path", REQUEST_PATHS)
@pytest.mark.parametrize("method", METHODS)
def test_a_static_map_hit_agrees_with_the_tree(shape, path, method):
    """Whenever the fast path answers, the slow path must answer the same.

    This is the property the two predicates jointly have to satisfy. If the
    exclusion in `_build_static_routes` ever stops mirroring the gate in
    `_match_tree`, a shape lands here.
    """
    router = _router(**SHAPES[shape])
    mapped = _map_info(router, method, path)
    if mapped is None:
        return
    assert _tree_info(router, method, path) is mapped, (shape, method, path)


@pytest.mark.parametrize("shape", list(SHAPES))
@pytest.mark.parametrize("path", REQUEST_PATHS)
@pytest.mark.parametrize("method", METHODS)
def test_match_agrees_with_the_tree_on_every_shape(shape, path, method):
    """The user-visible form of the same property: `match()` - which consults
    the map first - must never differ from walking the tree alone."""
    router = _router(**SHAPES[shape])
    combined = router.match(method, path)
    walked = _tree_info(router, method, path)
    assert (None if combined is None else combined.route_info) is walked, (shape, method, path)


# ── the map is not vacuously empty ───────────────────────────────────
#
# Every assertion above passes trivially if the map never holds anything.


def test_a_literal_route_reaches_the_static_map():
    router = _router(routes=(("/a", "GET"),))
    assert _map_info(router, "GET", "/a") is not None


def test_the_map_holds_a_nested_literal_too():
    router = _router(routes=(("/a/b", "GET"),))
    assert _map_info(router, "GET", "/a/b") is not None


def test_the_root_route_reaches_the_static_map():
    router = _router(routes=(("/", "GET"),))
    assert _map_info(router, "GET", "/") is not None


# ── the exclusions are real ──────────────────────────────────────────


def test_a_tolerant_route_is_excluded_from_the_map():
    """`strict_slashes=False` must fall through, or the map would answer for a
    slash shape the tree deliberately tolerates by a different route."""
    router = _router(routes=(("/a", "GET"),), strict_slashes=False)
    assert _map_info(router, "GET", "/a") is None


def test_a_slashed_route_is_excluded_from_the_map():
    router = _router(routes=(("/a/", "GET"),))
    assert _map_info(router, "GET", "/a/") is None


# ── and the shapes still route as documented ─────────────────────────


@pytest.mark.parametrize(
    ("shape", "path", "matches"),
    [
        ("unslashed", "/a", True),
        ("unslashed", "/a/", False),
        ("slashed", "/a/", True),
        ("slashed", "/a", False),
        ("both", "/a", True),
        ("tolerant", "/a", True),
        ("tolerant", "/a/", True),
    ],
)
def test_the_slash_semantics_are_unchanged(shape, path, matches):
    """The negative: an "agreement" achieved by making everything miss would
    satisfy the invariant tests above."""
    router = _router(**SHAPES[shape])
    assert (router.match("GET", path) is not None) is matches


def test_a_head_alias_in_the_map_agrees_with_the_tree():
    """The map adds a HEAD->GET alias the tree computes on the fly; the two must
    still land on the same RouteInfo."""
    router = _router(routes=(("/a", "GET"),))
    mapped = _map_info(router, "HEAD", "/a")
    assert mapped is not None
    assert _tree_info(router, "HEAD", "/a") is mapped


def test_the_app_level_router_holds_the_same_invariant():
    """Through the public surface, so the property is not only about `Router`."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return {}

    @app.get("/y/")
    async def y():
        return {}

    for path in ["/x", "/x/", "/y", "/y/"]:
        combined = app.match("GET", path)
        walked = _tree_info(app, "GET", path)
        assert (None if combined is None else combined.route_info) is walked, path
