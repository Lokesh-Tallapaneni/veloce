"""What the GraphQL how-to says about `app.mount` holds, with no GraphQL library.

The page mounts a GraphQL ASGI application at a prefix and makes two claims
about the mount itself, not about any GraphQL library: the matched prefix moves
from `path` to `root_path`, and a mount owns every path under its prefix.
"""

from __future__ import annotations

from veloce import Veloce
from veloce.testclient import TestClient


def _recording_asgi_app(seen: list[dict]):
    async def asgi_app(scope, receive, send):
        seen.append({"path": scope["path"], "root_path": scope.get("root_path", "")})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"mounted"})

    return asgi_app


def test_the_prefix_moves_from_path_to_root_path():
    seen: list[dict] = []
    app = Veloce(openapi_url=None)
    app.mount("/graphql", _recording_asgi_app(seen))

    response = TestClient(app).get("/graphql/query")

    assert response.status_code == 200
    assert seen == [{"path": "/query", "root_path": "/graphql"}]


def test_a_native_route_under_a_mount_prefix_is_unreachable():
    seen: list[dict] = []
    app = Veloce(openapi_url=None)

    @app.get("/graphql/health")
    async def health():
        return {"ok": True}

    app.mount("/graphql", _recording_asgi_app(seen))

    response = TestClient(app).get("/graphql/health")

    assert response.text == "mounted"
    assert seen and seen[0]["path"] == "/health"


def test_other_paths_fall_through_to_the_router():
    seen: list[dict] = []
    app = Veloce(openapi_url=None)

    @app.get("/status")
    async def status():
        return {"ok": True}

    app.mount("/graphql", _recording_asgi_app(seen))

    assert TestClient(app).get("/status").json() == {"ok": True}
    assert seen == []
