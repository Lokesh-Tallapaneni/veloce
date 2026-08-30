"""Which resolver an MCP tool call actually uses, pinned against a wrong claim.

A review finding read `dependency.py`'s compiled-graph gate - which skips the
compiled straight-line resolver when `_mcp_context` is set - and concluded that
"every MCP call permanently falls back to the slow interpreter, forfeiting the
2.8-3.9x". The comment beside that gate said as much, so the reading was fair.

It is wrong, and these tests are the evidence:

    HTTP request, 3-deep Depends chain   ->  plan.compiled_graph_resolver is a function
    MCP tool call, same chain            ->  plan.compiled_graph_resolver is None
                                             plan.compiled_resolver       is None

The MCP door never calls `DependencyResolver.resolve()`. It walks the top-level
slots itself in `plan_bridge.bind_arguments`, because a tool's arguments come
from a JSON object and `resolve()` reads query-string rules; it reaches the
resolver only through `_exec_depends` for each sub-graph. So the gate the finding
blamed is not what excludes MCP from the compiled path - the MCP door was never
on that path, context or no context - and setting `_mcp_context` costs it
nothing there.

Gating the context on whether a tool's sub-graph declares one was written and
measured: over six interleaved pairs the base arm's own spread was 27 us against
a 4.8 us difference, with two pairs running the other way. Not attributable, so
it was reverted rather than shipped as a win.

What these tests are for is the invariant itself. The claim was expensive to
disprove because nothing recorded which resolver each door uses; a change that
puts MCP on the compiled path, or takes HTTP off it, should announce itself here.
"""

from __future__ import annotations

import orjson
import pytest

from tests._mcp import initialize
from veloce import Depends, Veloce
from veloce.testclient import TestClient

pytest.importorskip("veloce.contrib.mcp")

from veloce.contrib.mcp.context import MCPContext  # noqa: E402


# Module scope: with `from __future__ import annotations`, a locally-defined
# dependency cannot be resolved by `get_type_hints`.
async def leaf():
    return 1


async def mid(a: int = Depends(leaf)):
    return a + 1


async def top(b: int = Depends(mid)):
    return b + 1


async def context_leaf(ctx: MCPContext):
    return ctx is not None


def _plan_of(app, template):
    for _method, _path, info in app._collect_all_routes(include_hidden=True):
        if info.path_template == template:
            return info.handler_plan
    raise AssertionError(template)


def _call_tool(app, arguments=None):
    client = TestClient(app)
    client.post(
        "/mcp",
        json=initialize(client_info={"name": "p", "version": "1"}),
        headers={"Accept": "application/json"},
    )
    listing = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json"},
    ).json()
    name = listing["result"]["tools"][0]["name"]
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        headers={"Accept": "application/json"},
    ).json()


def _result_of(payload):
    """The tool's returned value, decoded from its text content block."""
    assert "error" not in payload, payload
    return orjson.loads(payload["result"]["content"][0]["text"])


def _tool_app(handler_factory):
    app = Veloce(openapi_url=None)
    handler_factory(app)
    app.mount_mcp(transport="http", path="/mcp")
    return app


# ── which door compiles ──────────────────────────────────────────────


def test_an_http_request_compiles_its_dependency_graph():
    """The baseline the finding compared against, and it is real."""
    app = Veloce(openapi_url=None)

    @app.get("/h")
    async def route(v: int = Depends(top)) -> dict:
        return {"v": v}

    TestClient(app).get("/h")
    assert _plan_of(app, "/h").compiled_graph_resolver is not None


def test_an_mcp_tool_call_does_not_use_the_compiled_graph_resolver():
    """Not because a context disabled it - because this door does not go there."""

    def build(app):
        @app.get("/t", expose_as_mcp_tool=True, mcp_description="t")
        async def route(v: int = Depends(top)) -> dict:
            return {"v": v}

    app = _tool_app(build)
    _call_tool(app)
    assert _plan_of(app, "/t").compiled_graph_resolver is None


def test_an_mcp_tool_call_does_not_use_the_compiled_param_resolver_either():
    """Both compiled slots stay unset, which only the `resolve()` path fills."""

    def build(app):
        @app.get("/t", expose_as_mcp_tool=True, mcp_description="t")
        async def route(n: int = 1) -> dict:
            return {"n": n}

    app = _tool_app(build)
    _call_tool(app, {"n": 2})
    plan = _plan_of(app, "/t")
    assert plan.compiled_resolver is None
    assert plan.compiled_graph_resolver is None


def test_a_tool_with_no_context_does_not_compile_either():
    """The finding's implied control: removing the context changes nothing here."""

    def build(app):
        @app.get("/t", expose_as_mcp_tool=True, mcp_description="t")
        async def route(v: int = Depends(top)) -> dict:
            return {"v": v}

    app = _tool_app(build)
    _call_tool(app)
    assert _plan_of(app, "/t").compiled_graph_resolver is None


# ── and both doors still answer correctly ────────────────────────────


def test_a_tool_resolves_its_dependency_chain():
    def build(app):
        @app.get("/t", expose_as_mcp_tool=True, mcp_description="t")
        async def route(v: int = Depends(top)) -> dict:
            return {"v": v}

    assert _result_of(_call_tool(_tool_app(build))) == {"v": 3}


def test_the_http_door_resolves_the_same_chain_to_the_same_value():
    """The two doors agree on the answer even though they take different paths."""
    app = Veloce(openapi_url=None)

    @app.get("/h")
    async def route(v: int = Depends(top)) -> dict:
        return {"v": v}

    assert TestClient(app).get("/h").json() == {"v": 3}


def test_a_sub_dependency_receives_the_context():
    """The behaviour the `_mcp_context` machinery exists for."""

    def build(app):
        @app.get("/t", expose_as_mcp_tool=True, mcp_description="t")
        async def route(seen: bool = Depends(context_leaf)) -> dict:
            return {"seen": seen}

    assert _result_of(_call_tool(_tool_app(build))) == {"seen": True}


def test_a_top_level_context_parameter_receives_the_context():
    def build(app):
        @app.get("/t", expose_as_mcp_tool=True, mcp_description="t")
        async def route(ctx: MCPContext) -> dict:
            return {"seen": ctx is not None}

    assert _result_of(_call_tool(_tool_app(build))) == {"seen": True}


def test_a_parameter_merely_named_context_stays_an_agent_input():
    """Detection is by type, never by name - the standing rule."""

    def build(app):
        @app.get("/t", expose_as_mcp_tool=True, mcp_description="t")
        async def route(context: str = "default") -> dict:
            return {"context": context}

    assert _result_of(_call_tool(_tool_app(build), {"context": "given"})) == {"context": "given"}


def test_a_tool_with_arguments_and_a_dependency_resolves_both():
    def build(app):
        @app.get("/t", expose_as_mcp_tool=True, mcp_description="t")
        async def route(n: int = 1, v: int = Depends(top)) -> dict:
            return {"n": n, "v": v}

    assert _result_of(_call_tool(_tool_app(build), {"n": 7})) == {"n": 7, "v": 3}
