"""Tests for the radix tree router."""

import logging

import pytest

from veloce import Veloce
from veloce.routing.router import Router
from veloce.testclient import TestClient


@pytest.fixture
def router():
    r = Router()

    @r.get("/")
    async def index(request):
        return "index"

    @r.get("/users")
    async def users(request):
        return "users"

    @r.get("/users/{user_id}")
    async def get_user(user_id: int):
        return f"user {user_id}"

    @r.post("/users")
    async def create_user(request):
        return "created"

    @r.get("/users/{user_id}/posts/{post_id}")
    async def get_post(user_id: int, post_id: int):
        return f"user {user_id} post {post_id}"

    @r.get("/files/*")
    async def serve_file(request):
        return "file"

    return r


def test_match_root(router):
    match = router.match("GET", "/")
    assert match is not None


def test_match_static_path(router):
    match = router.match("GET", "/users")
    assert match is not None
    assert match.path_params == {}


def test_no_match(router):
    match = router.match("GET", "/nonexistent")
    assert match is None


def test_method_not_allowed(router):
    match = router.match("DELETE", "/users")
    assert match is None
    allowed = router.get_allowed_methods("/users")
    assert "GET" in allowed
    assert "POST" in allowed


class TestStaticRouteFastMap:
    """The literal-path fast map must stay behavior-identical to the radix tree
    and invalidate on every registration."""

    def test_literal_uses_fast_map_with_empty_params(self, router):
        match = router.match("GET", "/users")
        assert match is not None
        assert match.path_params == {}
        assert router._static_routes is not None
        assert ("GET", "/users") in router._static_routes

    def test_registration_invalidates_map(self, router):
        assert router.match("GET", "/users") is not None
        assert router._static_routes is not None

        @router.get("/late")
        async def late(request): ...

        assert router._static_routes is None
        match = router.match("GET", "/late")
        assert match is not None
        assert match.path_params == {}

    def test_param_route_falls_through(self, router):
        match = router.match("GET", "/users/42")
        assert match is not None
        assert match.path_params == {"user_id": "42"}

    def test_head_falls_back_to_get(self, router):
        assert router.match("HEAD", "/users") is not None

    def test_lowercase_method_resolves(self, router):
        assert router.match("get", "/users") is not None

    def test_trailing_slash_route_not_served_for_bare_path(self):
        r = Router()

        @r.get("/ts/")
        async def ts(request): ...

        assert r.match("GET", "/ts/") is not None
        assert r.match("GET", "/ts") is None


def test_single_param(router):
    match = router.match("GET", "/users/42")
    assert match is not None
    assert match.path_params == {"user_id": "42"}


def test_multiple_params(router):
    match = router.match("GET", "/users/1/posts/99")
    assert match is not None
    assert match.path_params == {"user_id": "1", "post_id": "99"}


def test_wildcard_match(router):
    match = router.match("GET", "/files/path/to/file.txt")
    assert match is not None
    assert match.path_params["_wildcard"] == "path/to/file.txt"


def test_include_router():
    main = Router()
    sub = Router(prefix="/api/v1")

    @sub.get("/items")
    async def items(request):
        return "items"

    main.include_router(sub)
    match = main.match("GET", "/api/v1/items")
    assert match is not None


def test_include_with_extra_prefix():
    main = Router()
    sub = Router(prefix="/v1")

    @sub.get("/data")
    async def data(request):
        return "data"

    main.include_router(sub, prefix="/api")
    match = main.match("GET", "/api/v1/data")
    assert match is not None


def test_all_methods():
    r = Router()

    @r.get("/test")
    async def get_handler(request): ...

    @r.post("/test")
    async def post_handler(request): ...

    @r.put("/test")
    async def put_handler(request): ...

    @r.patch("/test")
    async def patch_handler(request): ...

    @r.delete("/test")
    async def delete_handler(request): ...

    for method in ["GET", "POST", "PUT", "PATCH", "DELETE"]:
        assert r.match(method, "/test") is not None


def test_greedy_with_trailing_segment_uses_regex_fallback():
    # A greedy `:path` converter followed by a static suffix is not
    # tree-expressible, so it now routes through the regex fallback
    # instead of raising at registration.
    r = Router()

    @r.get("/{files:path}/info")
    async def handler(files):
        return files

    match = r.match("GET", "/a/b/c/info")
    assert match is not None
    assert match.path_params == {"files": "a/b/c"}


def test_greedy_as_final_segment_allowed():
    r = Router()

    @r.get("/{files:path}")
    async def serve(files: str):
        return files

    match = r.match("GET", "/a/b/c.txt")
    assert match is not None


def test_include_router_rejects_greedy_with_trailing_segments():
    sub = Router()

    # Smuggle the invalid shape past add_route by building it manually,
    # then verify _merge_node refuses to copy it in.
    async def handler(request): ...

    sub.add_route("/{files:path}", handler, ["GET"])
    # Tack on a static child after the greedy param to fabricate the
    # invalid shape that _merge_node must reject when re-walking.
    from veloce.routing.router import RadixNode

    greedy_node = sub._root.param_children[0]
    tail = RadixNode("info")
    greedy_node.static_children["info"] = tail
    tail.handlers = greedy_node.handlers
    greedy_node.handlers = {}

    main = Router()
    with pytest.raises(ValueError, match="greedy converter"):
        main.include_router(sub)


# ── Duplicate path-parameter detection ───────────────────────────────


def test_duplicate_param_on_tree_path_raises():
    r = Router()

    async def handler(request):
        return "x"

    with pytest.raises(ValueError, match="duplicate path parameter"):
        r.add_route("/{id}/x/{id}", handler, ["GET"])


def test_duplicate_param_on_regex_path_raises():
    r = Router()

    async def handler(request):
        return "x"

    # Partial-segment params force the regex branch; a clean ValueError must
    # replace the opaque re.PatternError ("redefinition of group name").
    with pytest.raises(ValueError, match="duplicate path parameter"):
        r.add_route("/files/{name}.{name}", handler, ["GET"])


def test_distinct_params_still_register():
    r = Router()

    async def handler(request):
        return "x"

    r.add_route("/{user_id}/posts/{post_id}", handler, ["GET"])
    # A valid route still registers fine after a rejected one on the same router.
    with pytest.raises(ValueError):
        r.add_route("/a/{k}/b/{k}", handler, ["GET"])
    r.add_route("/{only}", handler, ["GET"])


# ── a duplicate route name is reported ───────────────────────────
#
# Moved here from `test_unswept_scope_findings.py`, a module named for the audit
# batch that produced it rather than for the source it covers.


def test_a_duplicate_name_on_a_different_path_warns(caplog):
    """The defect: the reverse entry was replaced in silence."""
    app = Veloce(openapi_url=None)

    @app.get("/users", name="listing")
    async def users() -> dict:
        return {}

    with caplog.at_level(logging.WARNING):

        @app.get("/posts", name="listing")
        async def posts() -> dict:
            return {}

    assert any("listing" in r.getMessage() for r in caplog.records)


def test_the_warning_names_both_paths(caplog):
    app = Veloce(openapi_url=None)

    @app.get("/users", name="listing")
    async def users() -> dict:
        return {}

    with caplog.at_level(logging.WARNING):

        @app.get("/posts", name="listing")
        async def posts() -> dict:
            return {}

    message = " ".join(r.getMessage() for r in caplog.records)
    assert "/users" in message
    assert "/posts" in message


def test_replacing_a_route_at_the_same_path_stays_silent(caplog):
    """The name legitimately moves with the route it names."""
    app = Veloce(openapi_url=None, on_duplicate="override")

    @app.get("/users", name="listing")
    async def first() -> dict:
        return {}

    async def second() -> dict:
        return {}

    with caplog.at_level(logging.WARNING):
        app.get("/users", name="listing")(second)

    assert not [r for r in caplog.records if "name" in (r.getMessage()).lower()]
    assert app.url_for("listing") == "/users"


def test_two_distinct_names_are_silent(caplog):
    app = Veloce(openapi_url=None)

    with caplog.at_level(logging.WARNING):

        @app.get("/users", name="users")
        async def users() -> dict:
            return {}

        @app.get("/posts", name="posts")
        async def posts() -> dict:
            return {}

    assert caplog.records == []


def test_the_last_registration_still_wins():
    """Reporting it must not change which route the name resolves to."""
    app = Veloce(openapi_url=None)

    @app.get("/users", name="listing")
    async def users() -> dict:
        return {}

    @app.get("/posts", name="listing")
    async def posts() -> dict:
        return {}

    assert app.url_for("listing") == "/posts"


def test_both_routes_still_serve():
    """A name collision is a naming problem, not a routing one."""
    app = Veloce(openapi_url=None)

    @app.get("/users", name="listing")
    async def users() -> dict:
        return {"which": "users"}

    @app.get("/posts", name="listing")
    async def posts() -> dict:
        return {"which": "posts"}

    client = TestClient(app)
    assert client.get("/users").json() == {"which": "users"}
    assert client.get("/posts").json() == {"which": "posts"}


def test_a_blueprint_name_is_still_namespaced(caplog):
    """The merge path was already protected and must stay silent."""
    from veloce import Blueprint

    app = Veloce(openapi_url=None)

    @app.get("/users", name="listing")
    async def users() -> dict:
        return {}

    bp = Blueprint("shop", url_prefix="/shop")

    @bp.get("/posts", name="listing")
    async def posts() -> dict:
        return {}

    with caplog.at_level(logging.WARNING):
        app.register_blueprint(bp)

    assert app.url_for("listing") == "/users"
    assert app.url_for("shop.listing") == "/shop/posts"
