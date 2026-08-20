"""Benchmarks whose cost lives in a thread hop — measured as wall time.

Everything here crosses the thread-pool boundary, either because the handler
is a plain `def` (Veloce offloads sync handlers so they cannot block the event
loop) or because the work itself is offloaded (`GZipMiddleware` hands
compression to the executor).

That boundary is why these are separated from the CPU-simulation suite.
Simulation instruments instructions, not system calls, so the futex traffic of
a thread handoff is silently excluded: CodSpeed reports the gzip benchmark as
"2.7 ms" while noting that 32 system calls worth 257.5 ms were left out. A
number that omits the dominant cost of the thing being measured is worse than
no number, because it looks stable while the real cost moves underneath it.
Wall time measures the handoff.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from benchmarks.conftest import run_async
from veloce import Depends, GZipMiddleware, Veloce


class Item(BaseModel):
    name: str
    price: float


# Built once at import: constructing it per iteration would measure dict and
# f-string allocation instead of the path under test.
BIG_PAYLOAD: dict[str, Any] = {"rows": [{"id": i, "name": f"row-{i}"} for i in range(200)]}


def _sync_dependency() -> int:
    return 42


def _build_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/sync/plain")
    def sync_plain() -> dict:
        return {"message": "Hello, World!"}

    @app.get("/async/plain")
    async def async_plain() -> dict:
        return {"message": "Hello, World!"}

    @app.get("/sync/params")
    def sync_params(q: str = "x", limit: int = 10) -> dict:
        return {"q": q, "limit": limit}

    @app.get("/sync/dependency")
    def sync_dependency(dep: int = Depends(_sync_dependency)) -> dict:
        return {"dep": dep}

    @app.post("/sync/validated")
    def sync_validated(item: Item) -> dict:
        return {"name": item.name, "price": item.price}

    @app.post("/async/validated")
    async def async_validated(item: Item) -> dict:
        return {"name": item.name, "price": item.price}

    @app.get("/big")
    async def big() -> dict:
        return BIG_PAYLOAD

    return app


APP = _build_app()

GZIP_APP = _build_app()
GZIP_APP.add_middleware(GZipMiddleware(minimum_size=256))


def _scope(
    method: str = "GET",
    path: str = "/sync/plain",
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
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    run_async(app(scope, receive, send))
    return messages


def _status(messages: list[dict]) -> int:
    return next(m["status"] for m in messages if m["type"] == "http.response.start")


JSON_HEADERS = [(b"host", b"testserver"), (b"content-type", b"application/json")]
ITEM_BODY = b'{"name":"Widget","price":9.99}'
GZIP_HEADERS = [(b"host", b"testserver"), (b"accept-encoding", b"gzip")]

# One warm-up per shape: the first call resolves the compiled pipeline and
# spins up the executor's first worker thread, neither of which is per-request
# cost.
for _warm_scope, _warm_body in (
    (_scope(), b""),
    (_scope(path="/async/plain"), b""),
    (_scope(path="/sync/params", query_string=b"q=hello&limit=25"), b""),
    (_scope(path="/sync/dependency"), b""),
    (_scope("POST", "/sync/validated", headers=JSON_HEADERS), ITEM_BODY),
    (_scope("POST", "/async/validated", headers=JSON_HEADERS), ITEM_BODY),
):
    _drive(APP, _warm_scope, _warm_body)
_drive(GZIP_APP, _scope(path="/big", headers=GZIP_HEADERS))


# ── Sync vs async, same work ───────────────────────────────
# Paired so the difference is the thread hop and nothing else.


def test_sync_route(benchmark):
    """A plain `def` handler: dispatch plus the thread-pool offload."""
    messages = benchmark(lambda: _drive(APP, _scope()))
    assert _status(messages) == 200


def test_async_route(benchmark):
    """The same handler as `async def` — the offload's counterfactual."""
    messages = benchmark(lambda: _drive(APP, _scope(path="/async/plain")))
    assert _status(messages) == 200


def test_sync_route_with_params(benchmark):
    """Query coercion still happens on the loop; only the body is offloaded."""
    messages = benchmark(
        lambda: _drive(APP, _scope(path="/sync/params", query_string=b"q=hello&limit=25"))
    )
    assert _status(messages) == 200


def test_sync_route_with_dependency(benchmark):
    """A sync dependency and a sync handler: two offloads in one request."""
    messages = benchmark(lambda: _drive(APP, _scope(path="/sync/dependency")))
    assert _status(messages) == 200


def test_sync_route_with_body_validation(benchmark):
    """Validation runs on the loop, the handler body in the executor."""
    messages = benchmark(
        lambda: _drive(APP, _scope("POST", "/sync/validated", headers=JSON_HEADERS), ITEM_BODY)
    )
    assert _status(messages) == 200


def test_async_route_with_body_validation(benchmark):
    """Same validation without the hop, to price the offload against it."""
    messages = benchmark(
        lambda: _drive(APP, _scope("POST", "/async/validated", headers=JSON_HEADERS), ITEM_BODY)
    )
    assert _status(messages) == 200


# ── Offloaded work ─────────────────────────────────────────


def test_asgi_gzip_large_response(benchmark):
    """A 200-row payload compressed by `GZipMiddleware`.

    `GZipMiddleware` sends compression to the executor, so most of this
    benchmark's cost is the handoff rather than the deflate itself.
    """
    messages = benchmark(lambda: _drive(GZIP_APP, _scope(path="/big", headers=GZIP_HEADERS)))
    assert _status(messages) == 200
