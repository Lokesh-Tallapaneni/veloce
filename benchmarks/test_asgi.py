"""End-to-end ASGI benchmarks — the surface a real server drives.

`handle_request` covers dispatch; this module covers the layer above it:
the ASGI scope walk, body receive, response emit, and the in-memory test
client that most Veloce users exercise their apps through.
"""

from __future__ import annotations

from pydantic import BaseModel

from benchmarks.conftest import run_async
from veloce import GZipMiddleware, TestClient, Veloce


class Item(BaseModel):
    name: str
    price: float


def _build_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/json")
    async def json_hello() -> dict:
        return {"message": "Hello, World!"}

    @app.get("/users/{user_id:int}")
    async def user(user_id: int) -> dict:
        return {"user_id": user_id}

    @app.post("/items")
    async def create(item: Item) -> dict:
        return {"name": item.name, "price": item.price}

    @app.get("/big")
    async def big() -> dict:
        return {"rows": [{"id": i, "name": f"row-{i}"} for i in range(200)]}

    return app


ASGI_APP = _build_app()

GZIP_APP = _build_app()
GZIP_APP.add_middleware(GZipMiddleware(minimum_size=256))


def _scope(
    method: str = "GET",
    path: str = "/json",
    query_string: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query_string,
        "headers": headers or [(b"host", b"testserver")],
        "client": ("127.0.0.1", 51234),
        "server": ("testserver", 80),
    }


def _drive(app: Veloce, scope: dict, body: bytes = b"") -> list[dict]:
    """Run one ASGI request/response cycle and collect the sent messages."""
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    run_async(app(scope, receive, send))
    return messages


def _status(messages: list[dict]) -> int:
    return next(m["status"] for m in messages if m["type"] == "http.response.start")


# ── Raw ASGI round trips ───────────────────────────────────

# One warm-up per shape so the lazy OpenAPI setup, the first-request
# latch, and the compiled-pipeline caches are resolved before measuring.
_drive(ASGI_APP, _scope())


def test_asgi_json_round_trip(benchmark):
    """Scope in, response messages out — the full server-facing path."""
    messages = benchmark(lambda: _drive(ASGI_APP, _scope()))
    assert _status(messages) == 200


def test_asgi_path_param_round_trip(benchmark):
    """Same round trip through a typed path parameter."""
    messages = benchmark(lambda: _drive(ASGI_APP, _scope(path="/users/42")))
    assert _status(messages) == 200


POST_HEADERS = [(b"host", b"testserver"), (b"content-type", b"application/json")]
POST_BODY = b'{"name":"Widget","price":9.99}'
_drive(ASGI_APP, _scope("POST", "/items", headers=POST_HEADERS), POST_BODY)


def test_asgi_post_json_round_trip(benchmark):
    """POST with a body: receive, validate, respond."""
    messages = benchmark(
        lambda: _drive(ASGI_APP, _scope("POST", "/items", headers=POST_HEADERS), POST_BODY)
    )
    assert _status(messages) == 200


GZIP_HEADERS = [(b"host", b"testserver"), (b"accept-encoding", b"gzip")]
_drive(GZIP_APP, _scope(path="/big", headers=GZIP_HEADERS))


def test_asgi_gzip_large_response(benchmark):
    """A 200-row payload compressed by `GZipMiddleware`."""
    messages = benchmark(lambda: _drive(GZIP_APP, _scope(path="/big", headers=GZIP_HEADERS)))
    assert _status(messages) == 200


# ── In-memory test client ──────────────────────────────────

CLIENT = TestClient(_build_app())
CLIENT.get("/json")


def test_test_client_get(benchmark):
    """`TestClient.get` — what a user's own test suite pays per call."""
    response = benchmark(CLIENT.get, "/json")
    assert response.status_code == 200


def test_test_client_post_json(benchmark):
    """`TestClient.post` with a JSON body."""
    response = benchmark(lambda: CLIENT.post("/items", json={"name": "Widget", "price": 9.99}))
    assert response.status_code == 200
