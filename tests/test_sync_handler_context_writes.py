"""A write from a sync handler survives, on both doors.

`g` and `MCPContext.result_meta` were both backed by a ContextVar bound *lazily,
inside the getter*:

    store = self._ctx_var.get()
    if store is None:
        store = {}
        self._ctx_var.set(store)   # <- runs inside the handler

A sync (`def`) handler runs through `copy_context().run(...)` in a thread, so
that `set()` landed in the throwaway copy. Every other request-scoped facility -
`request.state`, `session`, `after_this_request` - mutates an object that already
exists and was unaffected. These two bound lazily, and these two broke:

    @app.after_request sees g.marker:
        async handler          set-in-async
        sync handler           <LOST>
        sync handler, but an unrelated before_request touched g first
                               set-in-sync

Two requests running *identical handler code* disagreed, because an unrelated
hook elsewhere in the request had bound the store in the request's own context.
The handler could not detect the loss: it read back what it wrote, inside the
thread. And `Depends(fn, offload=True)` - a documented knob - turned a working
sync dependency into a broken one.

Both slots are now bound eagerly, before the handler runs, so a write is a
mutation of a dict both contexts share. The cost is one empty dict per request,
measured neutral on the request path.
"""

from __future__ import annotations

import pytest

from veloce import Depends, MCPContext, Veloce, g
from veloce.testclient import TestClient

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}


def _seen(app: Veloce) -> dict:
    """Record what `after_request` sees, which is where the loss showed."""
    seen: dict = {}

    @app.after_request
    async def capture(response):
        seen["marker"] = g.get("marker", "<LOST>")
        return response

    app.state.seen = seen
    return seen


# ── an HTTP handler ──────────────────────────────────────────────────


def test_a_sync_handler_write_is_visible_after_the_request():
    """The defect: this read `<LOST>`."""
    app = Veloce(openapi_url=None)
    seen = _seen(app)

    @app.get("/x")
    def x() -> dict:
        g.marker = "set-in-sync"
        return {"ok": True}

    TestClient(app).get("/x")
    assert seen["marker"] == "set-in-sync"


def test_an_async_handler_write_is_still_visible():
    """The path that already worked."""
    app = Veloce(openapi_url=None)
    seen = _seen(app)

    @app.get("/x")
    async def x() -> dict:
        g.marker = "set-in-async"
        return {"ok": True}

    TestClient(app).get("/x")
    assert seen["marker"] == "set-in-async"


def test_the_two_handler_kinds_agree():
    """The property: how the handler was declared must not decide this."""
    results = {}
    for label, make in (("sync", False), ("async", True)):
        app = Veloce(openapi_url=None)
        seen = _seen(app)
        if make:

            @app.get("/x")
            async def x() -> dict:
                g.marker = "written"
                return {}
        else:

            @app.get("/x")
            def x() -> dict:  # type: ignore[misc]
                g.marker = "written"
                return {}

        TestClient(app).get("/x")
        results[label] = seen["marker"]
    assert results["sync"] == results["async"] == "written"


def test_an_unrelated_hook_touching_g_first_does_not_change_the_outcome():
    """The defect's tell: adding this hook used to flip the behaviour."""
    app = Veloce(openapi_url=None)
    seen = _seen(app)

    @app.before_request
    async def pretouch(request):
        g.get("anything")
        return None

    @app.get("/x")
    def x() -> dict:
        g.marker = "set-in-sync"
        return {}

    TestClient(app).get("/x")
    assert seen["marker"] == "set-in-sync"


# ── dependencies, including the offload knob ─────────────────────────


def _dep_app(offload: bool) -> tuple[Veloce, dict]:
    app = Veloce(openapi_url=None)
    seen = _seen(app)

    def mark() -> str:
        g.marker = "set-in-dependency"
        return "ok"

    @app.get("/x", dependencies=[Depends(mark, offload=offload)])
    async def x() -> dict:
        return {}

    return app, seen


@pytest.mark.parametrize("offload", [False, True])
def test_a_sync_dependency_write_survives_either_way(offload):
    """`offload=True` turned a working sync dependency into a broken one."""
    app, seen = _dep_app(offload)
    TestClient(app).get("/x")
    assert seen["marker"] == "set-in-dependency"


def test_the_offload_knob_does_not_change_the_outcome():
    outcomes = []
    for offload in (False, True):
        app, seen = _dep_app(offload)
        TestClient(app).get("/x")
        outcomes.append(seen["marker"])
    assert outcomes[0] == outcomes[1]


# ── g still behaves as before in every other respect ─────────────────


def test_g_does_not_leak_between_requests():
    """Eager binding must not make the store outlive its request."""
    app = Veloce(openapi_url=None)

    @app.get("/set")
    def set_it() -> dict:
        g.marker = "first"
        return {}

    @app.get("/read")
    def read_it() -> dict:
        return {"marker": g.get("marker", "<absent>")}

    client = TestClient(app)
    client.get("/set")
    assert client.get("/read").json()["marker"] == "<absent>"


def test_g_raises_for_an_unset_attribute():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    def x() -> dict:
        with pytest.raises(AttributeError):
            _ = g.never_set
        return {"ok": True}

    assert TestClient(app).get("/x").json() == {"ok": True}


def test_g_supports_pop_setdefault_and_contains_from_a_sync_handler():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    def x() -> dict:
        g.setdefault("a", 1)
        return {"in": "a" in g, "popped": g.pop("a"), "after": "a" in g}

    assert TestClient(app).get("/x").json() == {"in": True, "popped": 1, "after": False}


def test_g_still_works_inside_an_app_context():
    """The other binding path, which was always eager."""
    app = Veloce(openapi_url=None)
    with app.app_context():
        g.marker = "in-context"
        assert g.marker == "in-context"


# ── the MCP door: the same mechanism ─────────────────────────────────


def _mcp_app():
    app = Veloce(title="Meta", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Async tool")
    async def a_tool(ctx: MCPContext) -> str:
        ctx.result_meta["io.example/trace"] = "from-async"
        return "ok"

    @app.mcp_tool(description="Sync tool")
    def s_tool(ctx: MCPContext) -> str:
        ctx.result_meta["io.example/trace"] = "from-sync"
        return "ok"

    @app.mcp_tool(description="Sync tool that attaches nothing")
    def quiet(ctx: MCPContext) -> str:
        return "ok"

    app.mount_mcp(transport="http", path="/mcp")
    return app


def _call(app: Veloce, tool: str) -> dict:
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}},
        headers={"Accept": "application/json"},
    )
    return response.json()["result"]


def test_a_sync_tool_can_attach_result_meta():
    """The defect: the sync tool's `_meta` never reached the client."""
    assert _call(_mcp_app(), "s_tool")["_meta"] == {"io.example/trace": "from-sync"}


def test_an_async_tool_can_still_attach_result_meta():
    assert _call(_mcp_app(), "a_tool")["_meta"] == {"io.example/trace": "from-async"}


def test_both_tool_kinds_attach_meta():
    """The property: `def` versus `async def` must not decide this."""
    app = _mcp_app()
    assert set(_call(app, "s_tool")["_meta"]) == set(_call(app, "a_tool")["_meta"])


def test_a_tool_that_attaches_nothing_sends_no_meta():
    """Eager binding must not start emitting an empty `_meta` block."""
    assert "_meta" not in _call(_mcp_app(), "quiet")


def test_meta_does_not_leak_to_the_next_call():
    """One call's `_meta` must not reach the next."""
    app = _mcp_app()
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})

    def call(tool: str) -> dict:
        return client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}},
            headers={"Accept": "application/json"},
        ).json()["result"]

    call("s_tool")
    assert "_meta" not in call("quiet")


def test_the_tool_result_itself_is_unchanged():
    for tool in ("a_tool", "s_tool", "quiet"):
        assert _call(_mcp_app(), tool)["content"][0]["text"] == "ok"
