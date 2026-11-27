"""app.routes — introspection listing of registered routes."""

from __future__ import annotations

from veloce import Request, Veloce


class TestRoutesProperty:
    def test_routes_listing(self):
        app = Veloce(openapi_url=None)

        @app.get("/users", tags=["users"])
        async def list_users(request: Request):
            return []

        @app.post("/users", tags=["users"])
        async def create_user(request: Request):
            return {}

        routes = app.routes
        assert len(routes) >= 2
        paths = [r["path"] for r in routes]
        assert "/users" in paths
        methods = [r["method"] for r in routes]
        assert "GET" in methods
        assert "POST" in methods
