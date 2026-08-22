"""Publishing a mounted sub-application's MCP surface through its parent.

Mounting composed HTTP routes but not MCP primitives, so an app assembled from
sub-applications had one flat MCP registry per top-level `Veloce()` — the tools
its sub-apps defined were unreachable to an agent, and merging them by hand meant
copying each primitive type separately.

It is opt-in. Mounting an app for its HTTP routes must not silently hand an agent
everything that app can do.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp.composition import mount_namespace
from veloce.contrib.mcp.prompts import build_prompt_registry
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.resources import build_resource_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


def _sub(title: str = "Billing", uri: str = "inv://one") -> Veloce:
    app = Veloce(title=title, openapi_url=None)

    @app.mcp_tool(description="Raise an invoice")
    async def invoice(amount: int) -> dict:
        return {"amount": amount}

    @app.mcp_prompt(description="Chase a payment")
    async def dun() -> str:
        return "pay up"

    @app.get(
        "/inv",
        expose_as_mcp_resource=True,
        mcp_resource_uri=uri,
        mcp_description="An invoice",
    )
    async def inv() -> dict:
        return {"ok": True}

    return app


def _parent(expose: bool = True, prefix: str = "/billing") -> Veloce:
    app = Veloce(title="Main", openapi_url=None)

    @app.mcp_tool(description="Something the parent does")
    async def parent_tool() -> int:
        return 1

    app.mount(prefix, _sub(), expose_mcp=expose)
    return app


# ── Opt-in ───────────────────────────────────────────────────────────


def test_mounting_without_opting_in_publishes_nothing():
    """Mounting for HTTP routes must not widen the agent-facing surface."""
    app = _parent(expose=False)
    assert sorted(build_registry(app).tools) == ["parent_tool"]
    assert sorted(build_prompt_registry(app).prompts) == []
    assert sorted(build_resource_registry(app).resources) == []


def test_opting_in_publishes_the_sub_app_tools():
    assert sorted(build_registry(_parent()).tools) == ["billing_invoice", "parent_tool"]


def test_opting_in_publishes_the_sub_app_prompts():
    assert sorted(build_prompt_registry(_parent()).prompts) == ["billing_dun"]


def test_a_resource_keeps_its_uri():
    """A URI is the client-facing address of the thing, not a name to rewrite."""
    assert sorted(build_resource_registry(_parent()).resources) == ["inv://one"]


# ── Naming ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [("/billing", "billing"), ("/a/b", "a_b"), ("billing", "billing"), ("/", ""), ("", "")],
)
def test_the_mount_prefix_becomes_the_namespace(prefix: str, expected: str):
    assert mount_namespace(prefix) == expected


def test_a_root_mount_leaves_names_alone():
    app = Veloce(title="Root", openapi_url=None)
    app.mount("/", _sub(), expose_mcp=True)
    assert "invoice" in build_registry(app).tools


def test_two_sub_apps_may_each_define_the_same_tool_name():
    """The renaming is what makes composing independent apps possible."""
    app = Veloce(title="Two", openapi_url=None)
    app.mount("/billing", _sub(uri="inv://billing"), expose_mcp=True)
    app.mount("/orders", _sub(title="Orders", uri="inv://orders"), expose_mcp=True)
    assert sorted(build_registry(app).tools) == ["billing_invoice", "orders_invoice"]


def test_two_sub_apps_publishing_one_uri_is_reported():
    app = Veloce(title="Clash", openapi_url=None)
    app.mount("/billing", _sub(), expose_mcp=True)
    app.mount("/orders", _sub(title="Orders"), expose_mcp=True)
    with pytest.raises(ValueError, match="Duplicate MCP resource URI"):
        build_resource_registry(app)


# ── The merged tool actually works ───────────────────────────────────


async def test_a_mounted_tool_is_callable_through_the_parent():
    response = await MCPServer(_parent()).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "billing_invoice", "arguments": {"amount": 7}},
        },
        MCPSession(),
    )
    assert response["result"]["content"][0]["text"] == '{"amount":7}'


async def test_the_listing_advertises_the_prefixed_name():
    """The memoised entry must be rebuilt, not carried over from the sub-app."""
    response = await MCPServer(_parent()).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, MCPSession()
    )
    names = {entry["name"] for entry in response["result"]["tools"]}
    assert names == {"billing_invoice", "parent_tool"}


async def test_the_sub_app_keeps_its_own_unprefixed_registry():
    """Merging copies; it must not rename the primitives the sub-app owns."""
    sub = _sub()
    app = Veloce(title="Parent", openapi_url=None)
    app.mount("/billing", sub, expose_mcp=True)

    assert sorted(build_registry(app).tools) == ["billing_invoice"]
    assert sorted(build_registry(sub).tools) == ["invoice"]


# ── Nesting ──────────────────────────────────────────────────────────


def test_a_mount_inside_a_mount_composes():
    leaf = Veloce(title="Leaf", openapi_url=None)

    @leaf.mcp_tool(description="Deep tool")
    async def deep() -> int:
        return 1

    middle = Veloce(title="Middle", openapi_url=None)
    middle.mount("/inner", leaf, expose_mcp=True)

    top = Veloce(title="Top", openapi_url=None)
    top.mount("/outer", middle, expose_mcp=True)

    assert sorted(build_registry(top).tools) == ["outer_inner_deep"]


def test_a_nested_mount_that_did_not_opt_in_stops_there():
    leaf = Veloce(title="Leaf", openapi_url=None)

    @leaf.mcp_tool(description="Deep tool")
    async def deep() -> int:
        return 1

    middle = Veloce(title="Middle", openapi_url=None)
    middle.mount("/inner", leaf)  # not exposed

    top = Veloce(title="Top", openapi_url=None)
    top.mount("/outer", middle, expose_mcp=True)

    assert sorted(build_registry(top).tools) == []


# ── HTTP mounting is unaffected ──────────────────────────────────────


def test_the_sub_app_routes_still_serve_over_http():
    from veloce import TestClient

    sub = Veloce(title="Sub", openapi_url=None)

    @sub.get("/ping")
    async def ping() -> dict:
        return {"pong": True}

    app = Veloce(title="Parent", openapi_url=None)
    app.mount("/sub", sub, expose_mcp=True)

    assert TestClient(app).get("/sub/ping").json() == {"pong": True}
