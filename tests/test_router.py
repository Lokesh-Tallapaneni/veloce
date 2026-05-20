"""Tests for the radix tree router."""

import pytest

from veloce.routing.router import Router


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


class TestStaticRoutes:
    def test_match_root(self, router):
        match = router.match("GET", "/")
        assert match is not None

    def test_match_static_path(self, router):
        match = router.match("GET", "/users")
        assert match is not None
        assert match.path_params == {}

    def test_no_match(self, router):
        match = router.match("GET", "/nonexistent")
        assert match is None

    def test_method_not_allowed(self, router):
        match = router.match("DELETE", "/users")
        assert match is None
        allowed = router.get_allowed_methods("/users")
        assert "GET" in allowed
        assert "POST" in allowed


class TestPathParams:
    def test_single_param(self, router):
        match = router.match("GET", "/users/42")
        assert match is not None
        assert match.path_params == {"user_id": "42"}

    def test_multiple_params(self, router):
        match = router.match("GET", "/users/1/posts/99")
        assert match is not None
        assert match.path_params == {"user_id": "1", "post_id": "99"}


class TestWildcard:
    def test_wildcard_match(self, router):
        match = router.match("GET", "/files/path/to/file.txt")
        assert match is not None
        assert match.path_params["_wildcard"] == "path/to/file.txt"


class TestRouterInclusion:
    def test_include_router(self):
        main = Router()
        sub = Router(prefix="/api/v1")

        @sub.get("/items")
        async def items(request):
            return "items"

        main.include_router(sub)
        match = main.match("GET", "/api/v1/items")
        assert match is not None

    def test_include_with_extra_prefix(self):
        main = Router()
        sub = Router(prefix="/v1")

        @sub.get("/data")
        async def data(request):
            return "data"

        main.include_router(sub, prefix="/api")
        match = main.match("GET", "/api/v1/data")
        assert match is not None


class TestDecorators:
    def test_all_methods(self):
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
