"""Memory benchmarks — what the framework's long-lived structures cost.

These measure allocation, not speed, so they cover the things that are held
rather than the things that are fast: the compiled route table, a deep
dependency graph, the OpenAPI schema, and the per-connection objects a server
keeps alive for as long as a client is connected.

The per-connection ones are the point of the suite. A `WebSocket` is held for
the whole life of a connection, so its footprint multiplies by concurrency in
a way a per-request object never does, and it is slotted specifically to keep
that number small. A future attribute added without a matching `__slots__`
entry silently restores the per-instance `__dict__` and undoes it - invisible
in a wall-clock benchmark, obvious here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from veloce import Depends, Veloce
from veloce.http.request import Request
from veloce.websocket import WebSocket

DEPENDENCY_DEPTH = 100
ROUTE_COUNT = 100
CONNECTION_COUNT = 1_000


class Item(BaseModel):
    name: str
    price: float
    tags: list[str] = []


def _build_deep_dependency_app() -> Veloce:
    """An app whose endpoint sits behind a `DEPENDENCY_DEPTH`-deep chain."""
    app = Veloce(openapi_url=None)
    dependencies: dict[int, Callable[..., Any]] = {}

    def create_dependency(index: int) -> Callable[..., Any]:
        if index == DEPENDENCY_DEPTH:

            async def leaf() -> str:
                return str(index)

            leaf.__name__ = f"dependency_{index}"
            return leaf

        next_dependency = dependencies[index + 1]

        async def link(sub: str = Depends(next_dependency)) -> str:
            return f"{index} -> {sub}"

        link.__name__ = f"dependency_{index}"
        return link

    for index in reversed(range(DEPENDENCY_DEPTH + 1)):
        dependencies[index] = create_dependency(index)

    @app.get("/deep")
    async def deep(value: str = Depends(dependencies[0])) -> dict:
        return {"value": value}

    return app


def _build_route_table_app() -> Veloce:
    """`ROUTE_COUNT` routes, each with a typed parameter and a body model."""
    app = Veloce(openapi_url=None)

    for index in range(ROUTE_COUNT):

        @app.get(f"/resource-{index}/{{item_id:int}}")
        async def read(item_id: int, q: str = "", limit: int = 10) -> dict:
            return {"item_id": item_id, "q": q, "limit": limit}

        @app.post(f"/resource-{index}")
        async def create(item: Item) -> Item:
            return item

    return app


def test_deep_dependency_graph(benchmark):
    """The compiled plan for a 100-deep `Depends` chain, held per route."""
    app = benchmark(_build_deep_dependency_app)
    assert app is not None


def test_route_table(benchmark):
    """The radix tree plus one compiled handler plan per route."""
    app = benchmark(_build_route_table_app)
    assert app is not None


def test_openapi_schema(benchmark):
    """The generated schema, held on the app once built."""
    app = Veloce()

    for index in range(ROUTE_COUNT):

        @app.post(f"/documented-{index}")
        async def documented(item: Item) -> Item:
            return item

    def build_schema() -> dict:
        # `openapi()` memoizes onto `app.openapi_schema`, so without clearing it
        # every call after the first returns the cached dict and the benchmark
        # measures an attribute read - it reported 0ns over six million
        # iterations before this reset was added.
        app.openapi_schema = None
        return app.openapi()

    schema = benchmark(build_schema)
    assert "paths" in schema


def test_websocket_connections(benchmark):
    """`CONNECTION_COUNT` live `WebSocket` objects.

    A server holds one per open connection for the connection's whole life, so
    this is the number that multiplies by concurrency. `WebSocket` is slotted
    to keep it small; an attribute added without a `__slots__` entry would
    restore the per-instance dict and show up here as a step change.
    """

    async def _receive() -> dict:
        return {"type": "websocket.connect"}

    async def _send(message: dict) -> None:
        return None

    scope = {"type": "websocket", "path": "/ws", "headers": [], "query_string": b""}

    def build() -> list[WebSocket]:
        return [WebSocket.from_asgi(dict(scope), _receive, _send) for _ in range(CONNECTION_COUNT)]

    sockets = benchmark(build)
    assert len(sockets) == CONNECTION_COUNT
    assert not hasattr(sockets[0], "__dict__"), "WebSocket regained a per-instance __dict__"


def test_request_objects(benchmark):
    """`CONNECTION_COUNT` `Request` objects, the per-request equivalent.

    Short-lived rather than held, but the same slotting invariant applies and
    the same regression would be invisible elsewhere.
    """
    headers = [
        (b"host", b"testserver"),
        (b"user-agent", b"benchmark"),
        (b"accept", b"application/json"),
        (b"accept-encoding", b"gzip"),
    ]

    def build() -> list[Request]:
        return [
            Request(
                method="GET",
                path="/items/42",
                query_string="q=hello&limit=25",
                headers=headers,
                body=b"",
            )
            for _ in range(CONNECTION_COUNT)
        ]

    requests = benchmark(build)
    assert len(requests) == CONNECTION_COUNT
    assert not hasattr(requests[0], "__dict__"), "Request regained a per-instance __dict__"
