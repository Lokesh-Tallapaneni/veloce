"""F8 — WebSocket dependency-injection parity with the HTTP path.

WebSocket DI now runs through the shared HandlerPlan / DependencyResolver,
so WebSocket dependencies get the same `yield`-style teardown and
`Security` / `SecurityScopes` support that HTTP handlers already had.
"""

from __future__ import annotations

from pydantic import BaseModel

from veloce import BackgroundTasks, Depends, Response, Security, SecurityScopes, Veloce, WebSocket


class _Item(BaseModel):
    name: str


# ── yield-style dependency teardown ───────────────────────────────────


def test_websocket_yield_dependency_teardown_runs():
    """A `yield` dependency's teardown runs after the handler returns."""
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []

    def get_resource():
        events.append("setup")
        yield "res"
        events.append("teardown")

    @app.websocket("/ws")
    async def echo(ws, resource=Depends(get_resource)):
        await ws.accept()
        events.append(f"handler:{resource}")
        await ws.send_text(resource)
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as conn:
        assert conn.receive_text() == "res"

    assert events == ["setup", "handler:res", "teardown"]


def test_websocket_async_yield_dependency_teardown_runs():
    """An async-generator dependency's teardown runs too."""
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []

    async def get_resource():
        events.append("setup")
        yield "ares"
        events.append("teardown")

    @app.websocket("/ws")
    async def echo(ws, resource=Depends(get_resource)):
        await ws.accept()
        await ws.send_text(resource)
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as conn:
        assert conn.receive_text() == "ares"

    assert events == ["setup", "teardown"]


# ── Security / SecurityScopes ─────────────────────────────────────────


def test_websocket_security_scopes_injected():
    """`Security(..., scopes=[...])` propagates to a `SecurityScopes` param."""
    app = Veloce(debug=True, openapi_url=None)

    async def authorize(scopes: SecurityScopes):
        return scopes.scopes

    @app.websocket("/ws")
    async def echo(ws, perms=Security(authorize, scopes=["read", "write"])):
        await ws.accept()
        await ws.send_text(",".join(perms))
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as conn:
        assert conn.receive_text() == "read,write"


def test_websocket_security_dependency_resolved():
    """A plain `Security()` dependency resolves like `Depends()`."""
    app = Veloce(debug=True, openapi_url=None)

    async def current_user():
        return "carol"

    @app.websocket("/ws")
    async def echo(ws, user=Security(current_user)):
        await ws.accept()
        await ws.send_text(user)
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as conn:
        assert conn.receive_text() == "carol"


# ── dependency_overrides ──────────────────────────────────────────────


def test_websocket_dependency_override_applies():
    app = Veloce(debug=True, openapi_url=None)

    async def get_user():
        return "real"

    async def fake_user():
        return "fake"

    @app.websocket("/ws")
    async def echo(ws, user=Depends(get_user)):
        await ws.accept()
        await ws.send_text(user)
        await ws.close()

    app.dependency_overrides[get_user] = fake_user
    client = app.test_client()
    with client.websocket_connect("/ws") as conn:
        assert conn.receive_text() == "fake"


# ── WebSocket injection by annotation ─────────────────────────────────


def test_websocket_injected_by_annotation():
    """A parameter typed `WebSocket` receives the connection, whatever
    its name."""
    app = Veloce(debug=True, openapi_url=None)

    @app.websocket("/ws")
    async def echo(connection: WebSocket):
        await connection.accept()
        await connection.send_text("ok")
        await connection.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as conn:
        assert conn.receive_text() == "ok"


def test_websocket_dependency_receives_websocket_by_annotation():
    """A dependency of a WebSocket handler can itself receive the
    connection by `WebSocket` annotation."""
    app = Veloce(debug=True, openapi_url=None)

    async def origin_of(connection: WebSocket):
        return connection.path_params.get("room", "?")

    @app.websocket("/ws/{room}")
    async def echo(ws, room_name=Depends(origin_of)):
        await ws.accept()
        await ws.send_text(room_name)
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws/lobby") as conn:
        assert conn.receive_text() == "lobby"


# ── path parameters still resolve ─────────────────────────────────────


def test_websocket_path_param_still_injected():
    app = Veloce(debug=True, openapi_url=None)

    @app.websocket("/room/{room_id}")
    async def echo(ws, room_id: int):
        await ws.accept()
        await ws.send_text(f"{room_id}:{type(room_id).__name__}")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/room/7") as conn:
        assert conn.receive_text() == "7:int"


# ── HTTP-only parameter kinds are inert on WebSocket handlers ──────────


def test_websocket_background_tasks_param_is_inert():
    """A `BackgroundTasks` parameter is not injected into a WebSocket
    handler — its handler default applies, no crash."""
    app = Veloce(debug=True, openapi_url=None)

    @app.websocket("/ws")
    async def echo(ws, bg: BackgroundTasks = None):
        await ws.accept()
        await ws.send_text("inert" if bg is None else "injected")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as conn:
        assert conn.receive_text() == "inert"


def test_websocket_response_param_is_inert():
    """A `Response` parameter is not injected into a WebSocket handler."""
    app = Veloce(debug=True, openapi_url=None)

    @app.websocket("/ws")
    async def echo(ws, resp: Response = None):
        await ws.accept()
        await ws.send_text("inert" if resp is None else "injected")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as conn:
        assert conn.receive_text() == "inert"


def test_websocket_body_model_param_is_inert():
    """A Pydantic-model parameter is not treated as a request body on a
    WebSocket handler — its handler default applies."""
    app = Veloce(debug=True, openapi_url=None)

    @app.websocket("/ws")
    async def echo(ws, item: _Item = None):
        await ws.accept()
        await ws.send_text("inert" if item is None else "injected")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as conn:
        assert conn.receive_text() == "inert"


def test_websocket_independent_async_deps_run_in_parallel():
    """Two sibling Depends() on a WebSocket handler begin concurrently.

    Confirms `resolve_ws_plan` passes the precomputed parallel-dependency
    grouping into the resolver, like the HTTP path.

    **Proven structurally, not by a clock.** This compared two `time.monotonic()`
    samples against a 30 ms bound after two real 50 ms sleeps - a wall-clock
    threshold in the default suite, which is the class of test this project
    excludes behind the `perf` marker, and which fails under CI contention for
    reasons that have nothing to do with the code.

    Each dependency now waits at a shared rendezvous before returning. If
    resolution were sequential the first would wait for a sibling that had not
    started, and the test would **hang** rather than pass - so concurrency is
    proven by the test completing at all, with no sleep and no threshold.
    """
    import asyncio

    app = Veloce(debug=True, openapi_url=None)
    arrived: list[str] = []
    both_here = asyncio.Event()

    async def _rendezvous(name: str) -> str:
        arrived.append(name)
        if len(arrived) == 2:
            both_here.set()
        # `asyncio.Barrier` says this in one line but landed in 3.11; this
        # project supports 3.10.
        await asyncio.wait_for(both_here.wait(), timeout=5.0)
        return name

    async def slow_a():
        return await _rendezvous("a")

    async def slow_b():
        return await _rendezvous("b")

    @app.websocket("/ws-par")
    async def handler(ws, a=Depends(slow_a), b=Depends(slow_b)):
        await ws.accept()
        await ws.send_text(f"{a}{b}")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws-par") as conn:
        assert conn.receive_text() == "ab"
    assert sorted(arrived) == ["a", "b"]
    assert both_here.is_set()
