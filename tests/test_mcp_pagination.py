"""Cursor pagination over the MCP list methods.

A server with a large catalogue sends all of it in one response, and every byte
lands in the agent's context. `mount_mcp(page_size=...)` opts into the spec's
cursor: each list answers at most that many entries plus a `nextCursor` while
more remain.

It is opt-in because `nextCursor` is optional for clients too: a client that
ignores it reads only the first page, so a server that paginated uninvited would
hide the rest of its catalogue from every client that does.
"""

from __future__ import annotations

import orjson
import pytest

from tests._mcp import INVALID_PARAMS
from veloce import MCPContext, Veloce
from veloce.contrib.mcp.errors import InvalidParamsError
from veloce.contrib.mcp.pagination import decode_cursor, encode_cursor, paginate
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


def _tool(index: int):
    async def handler() -> int:
        return index

    handler.__name__ = f"tool_{index}"
    return handler


def _resource(index: int):
    async def handler() -> dict:
        return {"index": index}

    handler.__name__ = f"res_{index}"
    return handler


def _prompt(index: int):
    async def handler() -> str:
        return f"prompt {index}"

    handler.__name__ = f"prompt_{index}"
    return handler


def _app(tools: int = 5, resources: int = 0, prompts: int = 0) -> Veloce:
    app = Veloce(title="Paged", version="1.0.0", openapi_url=None)
    for index in range(tools):
        app.mcp_tool(description=f"Tool {index}")(_tool(index))
    for index in range(resources):
        app.get(
            f"/r{index}",
            expose_as_mcp_resource=True,
            mcp_resource_uri=f"res://{index}",
            mcp_description=f"Resource {index}",
        )(_resource(index))
    for index in range(prompts):
        app.mcp_prompt(description=f"Prompt {index}")(_prompt(index))
    return app


async def _list(server: MCPServer, method: str, cursor: str | None = None) -> dict:
    params: dict = {}
    if cursor is not None:
        params["cursor"] = cursor
    return await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, MCPSession()
    )


async def _walk(server: MCPServer, method: str, key: str) -> tuple[list[dict], int]:
    """Walk every page, returning the entries seen and the number of pages."""
    seen: list[dict] = []
    pages = 0
    cursor: str | None = None
    while True:
        result = (await _list(server, method, cursor))["result"]
        pages += 1
        seen.extend(result[key])
        cursor = result.get("nextCursor")
        if cursor is None:
            return seen, pages
        assert pages < 50, "pagination did not terminate"


# ── Off by default ───────────────────────────────────────────────────


async def test_a_list_is_answered_in_full_by_default():
    result = (await _list(MCPServer(_app(tools=5)), "tools/list"))["result"]
    assert len(result["tools"]) == 5
    assert "nextCursor" not in result


async def test_a_cursor_is_refused_when_the_server_does_not_paginate():
    """No cursor was ever issued, so one presented is not this server's."""
    response = await _list(MCPServer(_app()), "tools/list", encode_cursor(0, "tool_0"))
    assert response["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("size", [0, -1])
def test_a_non_positive_page_size_is_refused(size: int):
    with pytest.raises(ValueError, match="positive integer"):
        MCPServer(_app(), page_size=size)


# ── Walking a catalogue ──────────────────────────────────────────────


async def test_every_tool_is_seen_exactly_once_across_the_pages():
    server = MCPServer(_app(tools=7), page_size=3)
    entries, pages = await _walk(server, "tools/list", "tools")
    assert [e["name"] for e in entries] == [f"tool_{i}" for i in range(7)]
    assert pages == 3


async def test_a_page_holds_at_most_the_page_size():
    server = MCPServer(_app(tools=7), page_size=3)
    result = (await _list(server, "tools/list"))["result"]
    assert len(result["tools"]) == 3
    assert result["nextCursor"]


async def test_the_last_page_carries_no_next_cursor():
    server = MCPServer(_app(tools=4), page_size=2)
    first = (await _list(server, "tools/list"))["result"]
    second = (await _list(server, "tools/list", first["nextCursor"]))["result"]
    assert [t["name"] for t in second["tools"]] == ["tool_2", "tool_3"]
    assert "nextCursor" not in second


async def test_a_catalogue_smaller_than_a_page_is_one_page():
    result = (await _list(MCPServer(_app(tools=2), page_size=10), "tools/list"))["result"]
    assert len(result["tools"]) == 2
    assert "nextCursor" not in result


async def test_an_exactly_divisible_catalogue_does_not_offer_an_empty_page():
    """A cursor on the last full page would send the client back for nothing."""
    server = MCPServer(_app(tools=4), page_size=2)
    entries, pages = await _walk(server, "tools/list", "tools")
    assert len(entries) == 4
    assert pages == 2


async def test_an_empty_catalogue_pages_to_nothing():
    server = MCPServer(_app(tools=0), page_size=3)
    result = (await _list(server, "tools/list"))["result"]
    assert result["tools"] == []
    assert "nextCursor" not in result


async def test_a_page_size_of_one_walks_every_entry():
    server = MCPServer(_app(tools=3), page_size=1)
    entries, pages = await _walk(server, "tools/list", "tools")
    assert [e["name"] for e in entries] == ["tool_0", "tool_1", "tool_2"]
    assert pages == 3


# ── Every list method ────────────────────────────────────────────────


async def test_resources_list_paginates():
    server = MCPServer(_app(tools=0, resources=5), page_size=2)
    entries, pages = await _walk(server, "resources/list", "resources")
    assert sorted(e["uri"] for e in entries) == [f"res://{i}" for i in range(5)]
    assert pages == 3


async def test_prompts_list_paginates():
    server = MCPServer(_app(tools=0, prompts=5), page_size=2)
    entries, pages = await _walk(server, "prompts/list", "prompts")
    assert len({e["name"] for e in entries}) == 5
    assert pages == 3


async def test_resource_templates_list_paginates():
    app = Veloce(title="Templates", openapi_url=None)
    for index in range(3):

        def handler(name: str, _index: int = index) -> dict:
            return {"name": name}

        handler.__name__ = f"tpl_{index}"
        app.get(
            f"/t{index}/{{name}}",
            expose_as_mcp_resource=True,
            mcp_resource_uri=f"tpl://{index}/{{name}}",
            mcp_description="A template",
        )(handler)

    server = MCPServer(app, page_size=2)
    first = (await _list(server, "resources/templates/list"))["result"]
    assert len(first["resourceTemplates"]) == 2
    second = (await _list(server, "resources/templates/list", first["nextCursor"]))["result"]
    assert len(second["resourceTemplates"]) == 1
    assert "nextCursor" not in second


# ── With a visibility filter ─────────────────────────────────────────


async def test_a_hidden_tool_does_not_occupy_a_page_slot():
    """Filtering runs first, so a page is full of tools the caller can see."""

    def hide_odd(tool, principal) -> bool:
        return not tool.name.endswith(("1", "3", "5"))

    server = MCPServer(_app(tools=6), tool_filter=hide_odd, page_size=2)
    entries, _pages = await _walk(server, "tools/list", "tools")
    assert [e["name"] for e in entries] == ["tool_0", "tool_2", "tool_4"]
    first = (await _list(server, "tools/list"))["result"]
    assert [t["name"] for t in first["tools"]] == ["tool_0", "tool_2"]


# ── A catalogue that changes mid-walk ────────────────────────────────


async def test_a_tool_added_mid_walk_does_not_skip_an_entry():
    """The cursor names the last item, not just a position that has since moved."""
    server = MCPServer(_app(tools=6), page_size=2)
    first = (await _list(server, "tools/list"))["result"]
    assert [t["name"] for t in first["tools"]] == ["tool_0", "tool_1"]

    # Registering ahead of the walk shifts every later index by one.
    existing = dict(server.registry.tools)
    server.registry.tools.clear()
    server.registry.tools["aaa_new"] = next(iter(existing.values()))
    server.registry.tools.update(existing)

    second = (await _list(server, "tools/list", first["nextCursor"]))["result"]
    assert [t["name"] for t in second["tools"]] == ["tool_2", "tool_3"]


async def test_a_removed_anchor_does_not_replay_the_whole_catalogue():
    """The anchor is gone; the walk resumes near where it was, not at the start."""
    server = MCPServer(_app(tools=6), page_size=2)
    first = (await _list(server, "tools/list"))["result"]
    server.registry.tools.pop("tool_1")

    second = (await _list(server, "tools/list", first["nextCursor"]))["result"]
    names = [t["name"] for t in second["tools"]]
    assert names
    assert "tool_0" not in names


# ── The cursor itself ────────────────────────────────────────────────


def test_a_cursor_round_trips():
    assert decode_cursor(encode_cursor(4, "tool_4")) == (4, "tool_4")


def test_a_cursor_survives_a_key_containing_a_separator():
    """A resource URI carries colons; only the first one delimits the index."""
    assert decode_cursor(encode_cursor(2, "res://a:b/c")) == (2, "res://a:b/c")


def test_a_cursor_is_a_url_safe_ascii_token():
    cursor = encode_cursor(1, "tool_1")
    assert cursor.isascii()
    assert "/" not in cursor
    assert "+" not in cursor


@pytest.mark.parametrize("cursor", ["not-base64!!", "", "YWJj", "LTE6eA=="])
def test_a_malformed_cursor_is_invalid_params(cursor: str):
    with pytest.raises(InvalidParamsError):
        decode_cursor(cursor)


async def test_a_malformed_cursor_is_reported_over_the_wire():
    server = MCPServer(_app(tools=3), page_size=2)
    response = await _list(server, "tools/list", "not-a-cursor")
    assert response["error"]["code"] == INVALID_PARAMS


# ── The pager itself ─────────────────────────────────────────────────


def test_an_unpaginated_call_returns_the_items_untouched():
    """No page size means no copy and no slice - the same object goes back."""
    items = [1, 2, 3]
    page, cursor = paginate(items, str, None, None)
    assert page is items
    assert cursor is None


def test_the_pager_accepts_a_non_sequence_iterable():
    page, cursor = paginate(iter(["a", "b", "c"]), str, None, 2)
    assert list(page) == ["a", "b"]
    assert cursor is not None


# ── The context listings are hidden-aware, and unpaged ───────────────
#
# `MCPContext.list_resources` / `list_prompts` had their own builders, which
# applied scope narrowing but not the connection's hidden set - so a handler
# enumerating the catalogue contradicted what the client's own listing showed.
# Routing them through the one listing builder fixes that, but the builder pages,
# and a handler cannot ask again for the next page. So they pass `page_size=None`.


def _hiding_app() -> Veloce:
    app = Veloce(openapi_url=None)

    for name in ("a_one", "b_two", "c_three", "d_four"):

        def make(bound: str):
            async def render() -> str:
                return bound

            render.__name__ = bound
            return render

        app.mcp_prompt(description=f"prompt {name}")(make(name))

    @app.mcp_tool(description="Hide one prompt, then list what remains")
    async def probe(ctx: MCPContext) -> dict:
        await ctx.hide("b_two")
        return {"names": [p["name"] for p in ctx.list_prompts()]}

    return app


async def _probe(server: MCPServer, session: MCPSession) -> list[str]:
    out = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "probe", "arguments": {}},
        },
        session,
    )
    return orjson.loads(out["result"]["content"][0]["text"])["names"]


async def test_a_context_listing_omits_what_the_connection_hid():
    """The defect: the handler saw a prompt the client's own listing did not."""
    server = MCPServer(_hiding_app())
    assert "b_two" not in await _probe(server, MCPSession())


async def test_a_context_listing_matches_the_clients_listing():
    server = MCPServer(_hiding_app())
    session = MCPSession()
    via_context = await _probe(server, session)
    listing = await server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "prompts/list", "params": {}}, session
    )
    assert via_context == [p["name"] for p in listing["result"]["prompts"]]


async def test_a_context_listing_is_not_truncated_by_the_servers_page_size():
    """A handler cannot ask again for the next page, so it gets the whole catalogue."""
    server = MCPServer(_hiding_app(), page_size=2)
    names = await _probe(server, MCPSession())
    assert names == ["a_one", "c_three", "d_four"]


async def test_the_clients_listing_still_pages():
    """The override must not disable paging for the wire protocol."""
    server = MCPServer(_hiding_app(), page_size=2)
    listing = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "prompts/list", "params": {}}, MCPSession()
    )
    assert len(listing["result"]["prompts"]) == 2
    assert "nextCursor" in listing["result"]
