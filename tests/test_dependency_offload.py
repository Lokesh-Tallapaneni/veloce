"""`Depends(offload=True)` runs a blocking sync dependency off the loop.

A sync dependency that does blocking work (a DB driver call, `requests.get`)
stalls every in-flight request when it runs inline on the event loop. Opting
into `offload=True` routes that one call through the thread pool, mirroring how
sync route handlers are already offloaded. The default stays inline so trivial
pure functions keep their zero-overhead call.
"""

from __future__ import annotations

import threading

from veloce import Depends, Veloce, request
from veloce.testclient import TestClient


def test_offloaded_sync_dep_runs_off_the_handler_thread():
    """An `offload=True` sync dep executes on a worker thread, not the loop."""
    app = Veloce(openapi_url=None)
    captured: dict[str, int] = {}

    def blocking_dep() -> str:
        captured["dep_thread"] = threading.get_ident()
        return "value"

    @app.get("/")
    async def handler(dep: str = Depends(blocking_dep, offload=True)) -> dict:
        captured["handler_thread"] = threading.get_ident()
        return {"dep": dep}

    with TestClient(app) as client:
        resp = client.get("/")

    assert resp.status_code == 200
    assert resp.json() == {"dep": "value"}
    # The offloaded dep ran in the executor, off the async handler's thread.
    assert captured["dep_thread"] != captured["handler_thread"]


def test_inline_sync_dep_runs_on_the_handler_thread():
    """Without `offload`, a sync dep runs inline (same thread as the handler)."""
    app = Veloce(openapi_url=None)
    captured: dict[str, int] = {}

    def inline_dep() -> str:
        captured["dep_thread"] = threading.get_ident()
        return "value"

    @app.get("/")
    async def handler(dep: str = Depends(inline_dep)) -> dict:
        captured["handler_thread"] = threading.get_ident()
        return {"dep": dep}

    with TestClient(app) as client:
        resp = client.get("/")

    assert resp.status_code == 200
    # The inline dep shares the async handler's thread (no executor hop).
    assert captured["dep_thread"] == captured["handler_thread"]


def test_offloaded_dep_sees_request_context():
    """Request-scoped context vars stay readable inside the offloaded dep."""
    app = Veloce(openapi_url=None)

    def needs_request() -> str:
        # `request` is a ContextVar-backed proxy; the offload path must
        # snapshot the context so this resolves inside the worker thread.
        return request.method

    @app.post("/")
    async def handler(method: str = Depends(needs_request, offload=True)) -> dict:
        return {"method": method}

    with TestClient(app) as client:
        resp = client.post("/")

    assert resp.status_code == 200
    assert resp.json() == {"method": "POST"}


def test_offload_flag_recorded_only_for_plain_sync_callables():
    """The plan records `dep_offload` only when it can take effect."""
    from veloce._handler_plan import build_plan

    async def coro_dep() -> str:
        return "a"

    def sync_dep() -> str:
        return "b"

    async def handler(
        a: str = Depends(coro_dep, offload=True),
        b: str = Depends(sync_dep, offload=True),
        c: str = Depends(sync_dep),
    ) -> None:
        return None

    plan = build_plan(handler)
    by_name = {s.name: s for s in plan.slots}
    # A coroutine dep ignores the flag (it already runs on the loop).
    assert by_name["a"].dep_offload is False
    # A plain sync dep with offload=True records it.
    assert by_name["b"].dep_offload is True
    # Default sync dep stays inline.
    assert by_name["c"].dep_offload is False
