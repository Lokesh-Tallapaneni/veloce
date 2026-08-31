"""Caller-scoped listing visibility.

The tool set a caller is shown may narrow by the authorization presented on the
request; it must never narrow by connection state, and hiding a tool must never
change what happens when it is called. These tests pin both halves.

Narrowing has two sources. Declared scopes always apply, to every listing: a
caller that would be refused a tool, prompt or resource is not shown it. A
`tool_filter` narrows what is left of the tool list, by whatever policy the
application has.
"""

from __future__ import annotations

import functools
from typing import Any

from tests._mcp import FORBIDDEN, call, call_error
from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.principal import Principal, set_principal


def _app() -> Veloce:
    app = Veloce(title="VisibilityProbe", openapi_url=None)

    @app.mcp_tool(description="Public reader")
    async def read_public() -> dict:
        return {"ok": True}

    @app.mcp_tool(description="Admin deleter", scopes=["admin"])
    async def delete_tenant() -> dict:
        return {"deleted": True}

    @app.mcp_tool(description="Billing reader", scopes=["billing"])
    async def read_invoice() -> dict:
        return {"amount": 1}

    return app


def _names(result: dict[str, Any]) -> set[str]:
    return {tool["name"] for tool in result["tools"]}


async def _list(server: MCPServer) -> dict[str, Any]:
    """The listing as a client sees it: through the dispatch map, not past it.

    Calling `server._handle_tools_list` directly skips the map, the in-flight
    tracking and the error shaping, so a method registered under the wrong name
    passed here and failed for a real client.
    """
    return await call(server, "tools/list")


# ── Declared scopes, with no filter configured ───────────────────────


async def test_a_caller_holding_every_scope_sees_every_tool():
    server = MCPServer(_app())
    set_principal(Principal(subject="root", scopes=frozenset({"admin", "billing"})))
    assert _names(await _list(server)) == {"read_public", "delete_tenant", "read_invoice"}


async def test_a_scoped_tool_is_not_listed_to_a_caller_that_cannot_invoke_it():
    """Listing it spends the agent's context on a call that can only be refused."""
    server = MCPServer(_app())
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    assert _names(await _list(server)) == {"read_public"}


async def test_an_unauthenticated_caller_sees_only_what_needs_no_scope():
    server = MCPServer(_app())
    assert _names(await _list(server)) == {"read_public"}


async def test_a_no_op_filter_shows_the_same_set_as_no_filter():
    """A policy that hides nothing must not change what a caller is shown."""
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    unfiltered = _names(await _list(MCPServer(_app())))
    permissive = _names(await _list(MCPServer(_app(), tool_filter=lambda tool, principal: True)))
    assert unfiltered == permissive


async def test_a_server_declaring_no_scopes_lists_everything():
    """Nothing to narrow, so the listing stays the plain build it always was."""
    app = Veloce(title="Open", openapi_url=None)

    @app.mcp_tool(description="Anyone")
    async def anyone() -> dict:
        return {}

    set_principal(Principal(subject="nobody", scopes=frozenset()))
    assert _names(await _list(MCPServer(app))) == {"anyone"}


# ── Scope filtering, active once a filter is configured ──────────────


async def test_filter_applies_declared_scopes_before_the_policy():
    """A caller lacking a tool's scope never sees it, whatever the policy says."""
    server = MCPServer(_app(), tool_filter=lambda tool, principal: True)
    set_principal(Principal(subject="reader", scopes=frozenset()))
    assert _names(await _list(server)) == {"read_public"}


async def test_caller_sees_exactly_the_scopes_it_holds():
    server = MCPServer(_app(), tool_filter=lambda tool, principal: True)
    set_principal(Principal(subject="ops", scopes=frozenset({"admin"})))
    assert _names(await _list(server)) == {"read_public", "delete_tenant"}


async def test_all_scopes_held_lists_everything():
    server = MCPServer(_app(), tool_filter=lambda tool, principal: True)
    set_principal(Principal(subject="root", scopes=frozenset({"admin", "billing"})))
    assert _names(await _list(server)) == {"read_public", "delete_tenant", "read_invoice"}


# ── The policy narrows further ───────────────────────────────────────


async def test_policy_can_hide_a_tool_the_scopes_allow():
    def only_reads(tool: Any, principal: Any) -> bool:
        return tool.name.startswith("read_")

    server = MCPServer(_app(), tool_filter=only_reads)
    set_principal(Principal(subject="root", scopes=frozenset({"admin", "billing"})))
    assert _names(await _list(server)) == {"read_public", "read_invoice"}


async def test_policy_cannot_reveal_a_tool_the_scopes_reject():
    """Filter-only: returning True for everything cannot widen the scoped set."""
    server = MCPServer(_app(), tool_filter=lambda tool, principal: True)
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    assert "delete_tenant" not in _names(await _list(server))


async def test_policy_receives_the_tool_and_principal():
    seen: list[tuple[str, str | None]] = []

    def record(tool: Any, principal: Any) -> bool:
        seen.append((tool.name, getattr(principal, "subject", None)))
        return True

    server = MCPServer(_app(), tool_filter=record)
    set_principal(Principal(subject="ada", scopes=frozenset({"admin", "billing"})))
    await _list(server)
    assert ("read_public", "ada") in seen
    assert {name for name, _ in seen} == {"read_public", "delete_tenant", "read_invoice"}


async def test_policy_can_read_route_tags_for_group_policies():
    """Tags propagate from a Blueprint, so one rule governs a whole group."""
    app = Veloce(title="TagProbe", openapi_url=None)

    @app.get("/admin/purge", expose_as_mcp_tool=True, mcp_description="Purge", tags=["admin"])
    async def purge() -> dict:
        return {"purged": True}

    @app.get("/status", expose_as_mcp_tool=True, mcp_description="Status", tags=["public"])
    async def statuspage() -> dict:
        return {"up": True}

    def by_tag(tool: Any, principal: Any) -> bool:
        tags = set(getattr(tool.route_info, "tags", ()) or ())
        return "admin" not in tags

    server = MCPServer(app, tool_filter=by_tag)
    assert _names(await _list(server)) == {"statuspage"}


# ── Async policies ───────────────────────────────────────────────────


async def test_async_policy_is_awaited():
    async def only_public(tool: Any, principal: Any) -> bool:
        return tool.name == "read_public"

    server = MCPServer(_app(), tool_filter=only_public)
    set_principal(Principal(subject="root", scopes=frozenset({"admin", "billing"})))
    assert _names(await _list(server)) == {"read_public"}


async def test_an_async_callable_object_policy_is_awaited():
    """A bare `iscoroutinefunction` misses an `async def __call__`.

    Misreading one as synchronous runs it in a worker thread, where it returns an
    un-awaited coroutine. Every coroutine is truthy, so the policy would admit
    every tool - failing open rather than closed.
    """

    class OnlyPublic:
        async def __call__(self, tool: Any, principal: Any) -> bool:
            return tool.name == "read_public"

    server = MCPServer(_app(), tool_filter=OnlyPublic())
    set_principal(Principal(subject="root", scopes=frozenset({"admin", "billing"})))
    assert _names(await _list(server)) == {"read_public"}


async def test_a_partially_applied_async_policy_is_awaited():
    """`functools.partial` around an `async def` must not read as synchronous."""

    async def only(tool: Any, principal: Any, *, keep: str) -> bool:
        return tool.name == keep

    server = MCPServer(_app(), tool_filter=functools.partial(only, keep="read_public"))
    set_principal(Principal(subject="root", scopes=frozenset({"admin", "billing"})))
    assert _names(await _list(server)) == {"read_public"}


async def test_a_synchronous_callable_object_policy_still_applies():
    class OnlyPublic:
        def __call__(self, tool: Any, principal: Any) -> bool:
            return tool.name == "read_public"

    server = MCPServer(_app(), tool_filter=OnlyPublic())
    set_principal(Principal(subject="root", scopes=frozenset({"admin", "billing"})))
    assert _names(await _list(server)) == {"read_public"}


async def test_policy_returning_a_truthy_non_bool_is_coerced():
    server = MCPServer(_app(), tool_filter=lambda tool, principal: tool.name.count("read"))
    set_principal(Principal(subject="root", scopes=frozenset({"admin", "billing"})))
    assert _names(await _list(server)) == {"read_public", "read_invoice"}


# ── Hiding is not enforcement ────────────────────────────────────────


async def test_hidden_tool_still_raises_authorization_error_when_called():
    """A tool hidden from listing is not thereby callable, nor silently missing."""
    server = MCPServer(_app(), tool_filter=lambda tool, principal: False)
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    assert _names(await _list(server)) == set()
    error = await call_error(server, "tools/call", {"name": "delete_tenant", "arguments": {}})
    assert error["code"] == FORBIDDEN


async def test_a_policy_hidden_but_in_scope_tool_remains_callable():
    """The policy governs visibility only; invocation still answers to scopes."""
    server = MCPServer(_app(), tool_filter=lambda tool, principal: False)
    set_principal(Principal(subject="ops", scopes=frozenset({"admin"})))
    assert _names(await _list(server)) == set()
    result = await call(server, "tools/call", {"name": "delete_tenant", "arguments": {}})
    assert result.get("isError") is not True
    assert "deleted" in result["content"][0]["text"]


# ── Transports without a per-request credential ──────────────────────


async def test_no_principal_still_lists_unscoped_tools():
    """stdio has no per-request credential; it must not list nothing."""
    server = MCPServer(_app(), tool_filter=lambda tool, principal: True)
    set_principal(None)
    assert _names(await _list(server)) == {"read_public"}


# ── Shape and ordering ───────────────────────────────────────────────


async def test_filtering_preserves_registration_order():
    server = MCPServer(_app(), tool_filter=lambda tool, principal: True)
    set_principal(Principal(subject="root", scopes=frozenset({"admin", "billing"})))
    listed = [tool["name"] for tool in (await _list(server))["tools"]]
    assert listed == ["read_public", "delete_tenant", "read_invoice"]


async def test_filtered_entries_keep_their_full_shape():
    server = MCPServer(_app(), tool_filter=lambda tool, principal: True)
    entry = (await _list(server))["tools"][0]
    assert entry["name"] == "read_public"
    assert entry["description"] == "Public reader"
    assert entry["inputSchema"]["type"] == "object"


async def test_no_cache_hints_are_advertised_while_filtering():
    """A shared cache scope would let one caller's list serve another."""
    server = MCPServer(_app(), tool_filter=lambda tool, principal: True)
    result = await _list(server)
    assert "cacheScope" not in result
    assert "ttlMs" not in result


def test_mount_mcp_accepts_a_tool_filter():
    app = _app()
    coro = app.mount_mcp(tool_filter=lambda tool, principal: True)
    coro.close()


# ── The same rule for prompts and resources ──────────────────────────


def _primitives_app() -> Veloce:
    app = Veloce(title="PrimitiveProbe", openapi_url=None)

    @app.mcp_prompt(description="Open prompt")
    async def open_prompt() -> str:
        return "hi"

    @app.mcp_prompt(description="Admin prompt", scopes=["admin"])
    async def admin_prompt() -> str:
        return "shh"

    @app.get(
        "/open",
        expose_as_mcp_resource=True,
        mcp_resource_uri="doc://open",
        mcp_description="Open document",
    )
    async def open_doc() -> dict:
        return {}

    @app.get(
        "/admin",
        expose_as_mcp_resource=True,
        mcp_resource_uri="doc://admin",
        mcp_description="Admin document",
        mcp_scopes=["admin"],
    )
    async def admin_doc() -> dict:
        return {}

    @app.get(
        "/admin/{name}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="doc://admin/{name}",
        mcp_description="Admin template",
        mcp_scopes=["admin"],
    )
    async def admin_template(name: str) -> dict:
        return {}

    return app


async def test_a_scoped_prompt_is_not_listed_to_a_caller_that_cannot_render_it():
    server = MCPServer(_primitives_app())
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    result = await call(server, "prompts/list")
    assert [prompt["name"] for prompt in result["prompts"]] == ["open_prompt"]


async def test_a_scoped_resource_is_not_listed_to_a_caller_that_cannot_read_it():
    server = MCPServer(_primitives_app())
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    result = await call(server, "resources/list")
    assert [resource["uri"] for resource in result["resources"]] == ["doc://open"]


async def test_a_scoped_resource_template_is_not_listed_either():
    server = MCPServer(_primitives_app())
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    result = await call(server, "resources/templates/list")
    assert result["resourceTemplates"] == []


async def test_a_caller_holding_the_grant_is_shown_all_three():
    server = MCPServer(_primitives_app())
    set_principal(Principal(subject="ops", scopes=frozenset({"admin"})))
    prompts = await call(server, "prompts/list")
    resources = await call(server, "resources/list")
    templates = await call(server, "resources/templates/list")
    assert len(prompts["prompts"]) == 2
    assert len(resources["resources"]) == 2
    assert len(templates["resourceTemplates"]) == 1


async def test_hiding_a_prompt_does_not_change_what_rendering_it_does():
    """Same contract as a tool: the listing narrows, the refusal is unchanged."""
    server = MCPServer(_primitives_app())
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    error = await call_error(server, "prompts/get", {"name": "admin_prompt"})
    assert error["code"] == FORBIDDEN


async def test_hiding_a_resource_does_not_change_what_reading_it_does():
    server = MCPServer(_primitives_app())
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    error = await call_error(server, "resources/read", {"uri": "doc://admin"})
    assert error["code"] == FORBIDDEN
