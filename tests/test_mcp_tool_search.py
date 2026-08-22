"""Serving a large catalogue through search instead of a listing.

Every entry `tools/list` returns lands in the agent's context window, and
pagination only spreads the same cost over more round trips. `tool_search=True`
publishes three tools in place of the catalogue: one to find tools, one to read
their definitions, and one to run several calls in a single request.

`run_tools` executes declared calls, never code: each step names a registered
tool and its arguments, and a step's argument may reference an earlier step's
result by JSON pointer. Nothing is compiled and no expression is evaluated, so
no sandbox is involved.
"""

from __future__ import annotations

import orjson
import pytest

from veloce import Principal, Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.principal import set_principal

META = {"search_tools", "describe_tools", "run_tools"}


def _app() -> Veloce:
    app = Veloce(title="Shop", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Find a customer by their email address")
    async def find_customer(email: str) -> dict:
        return {"id": 42, "email": email}

    @app.mcp_tool(description="List the orders placed by a customer")
    async def list_orders(customer_id: int) -> dict:
        return {"orders": [{"id": "A-1"}, {"id": "A-2"}]}

    @app.mcp_tool(description="Refund an order in full")
    async def refund_order(order_id: str) -> dict:
        return {"refunded": order_id}

    @app.mcp_tool(description="Delete every record", scopes=["admin"])
    async def wipe() -> dict:
        return {"gone": True}

    return app


def _server(**kwargs) -> MCPServer:
    return MCPServer(_app(), tool_search=True, **kwargs)


async def _list(server: MCPServer) -> list[str]:
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, MCPSession()
    )
    return [tool["name"] for tool in response["result"]["tools"]]


async def _call(server: MCPServer, name: str, arguments: dict) -> tuple[bool, object]:
    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        MCPSession(),
    )
    result = response["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        return True, text
    return False, orjson.loads(text)


async def _run(server: MCPServer, steps: list[dict], **kwargs) -> tuple[bool, object]:
    return await _call(server, "run_tools", {"steps": steps, **kwargs})


# ── What the catalogue looks like ────────────────────────────────────


async def test_only_the_search_tools_are_listed():
    """The whole point: four tools must not cost four descriptions."""
    assert set(await _list(_server())) == META


async def test_a_server_without_it_still_lists_everything():
    assert "refund_order" in await _list(MCPServer(_app()))


async def test_a_tool_that_is_not_listed_is_still_callable_directly():
    """Search replaces discovery, not invocation."""
    is_error, payload = await _call(_server(), "refund_order", {"order_id": "A-1"})
    assert (is_error, payload) == (False, {"refunded": "A-1"})


async def test_the_read_only_tools_say_so():
    server = _server()
    entries = {
        tool.name: server._describe_tool(tool)
        for tool in server.registry.tools.values()
        if tool.name in META
    }
    assert entries["search_tools"]["annotations"]["readOnlyHint"] is True
    assert entries["run_tools"]["annotations"]["readOnlyHint"] is False


# ── Finding a tool ───────────────────────────────────────────────────


async def test_a_tool_is_found_by_a_word_from_its_description():
    _is_error, found = await _call(_server(), "search_tools", {"query": "refund an order"})
    assert found[0]["name"] == "refund_order"


async def test_the_match_carries_what_the_agent_needs_to_choose():
    _is_error, found = await _call(_server(), "search_tools", {"query": "refund"})
    assert found[0]["description"] == "Refund an order in full"
    assert found[0]["score"] > 0


async def test_a_query_matching_nothing_finds_nothing():
    _is_error, found = await _call(_server(), "search_tools", {"query": "photosynthesis"})
    assert found == []


async def test_the_limit_is_respected():
    _is_error, found = await _call(
        _server(), "search_tools", {"query": "customer order", "limit": 1}
    )
    assert len(found) == 1


async def test_the_search_tools_do_not_find_themselves():
    """Listing them again would spend the context the search saved."""
    _is_error, found = await _call(_server(), "search_tools", {"query": "tools run search"})
    assert {match["name"] for match in found}.isdisjoint(META)


async def test_a_tool_the_caller_may_not_invoke_is_not_found():
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    _is_error, found = await _call(_server(), "search_tools", {"query": "delete every record"})
    assert found == []


async def test_a_caller_holding_the_grant_finds_it():
    set_principal(Principal(subject="ops", scopes=frozenset({"admin"})))
    _is_error, found = await _call(_server(), "search_tools", {"query": "delete every record"})
    assert found[0]["name"] == "wipe"


async def test_a_visibility_policy_still_decides_what_is_findable():
    server = _server(tool_filter=lambda tool, principal: tool.name != "refund_order")
    _is_error, found = await _call(server, "search_tools", {"query": "refund an order"})
    assert found == []


# ── Reading a definition ─────────────────────────────────────────────


async def test_a_named_tool_is_described_in_full():
    _is_error, described = await _call(_server(), "describe_tools", {"names": ["refund_order"]})
    assert described[0]["inputSchema"]["properties"]["order_id"]["type"] == "string"


async def test_several_tools_are_described_in_the_order_asked_for():
    _is_error, described = await _call(
        _server(), "describe_tools", {"names": ["refund_order", "find_customer"]}
    )
    assert [entry["name"] for entry in described] == ["refund_order", "find_customer"]


async def test_describing_a_tool_that_does_not_exist_says_where_to_look():
    is_error, text = await _call(_server(), "describe_tools", {"names": ["invented"]})
    assert is_error is True
    assert "search_tools" in text


async def test_a_tool_the_caller_may_not_invoke_is_not_described():
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    is_error, _text = await _call(_server(), "describe_tools", {"names": ["wipe"]})
    assert is_error is True


# ── Running a plan ───────────────────────────────────────────────────


async def test_each_step_runs_and_is_reported():
    _is_error, run = await _run(
        _server(),
        [
            {"tool": "find_customer", "arguments": {"email": "ada@example.com"}},
            {"tool": "refund_order", "arguments": {"order_id": "A-1"}},
        ],
    )
    assert [step["tool"] for step in run["steps"]] == ["find_customer", "refund_order"]


async def test_an_unnamed_step_is_reported_under_its_position():
    _is_error, run = await _run(
        _server(), [{"tool": "refund_order", "arguments": {"order_id": "A"}}]
    )
    assert run["steps"][0]["id"] == "step1"


async def test_a_step_reads_an_earlier_result_by_pointer():
    """The chain the round trip exists to avoid."""
    _is_error, run = await _run(
        _server(),
        [
            {"id": "cust", "tool": "find_customer", "arguments": {"email": "ada@example.com"}},
            {
                "tool": "list_orders",
                "arguments": {"customer_id": {"$from": "cust", "path": "/id"}},
            },
        ],
    )
    assert (
        run["steps"][1]["result"]["content"][0]["text"] == '{"orders":[{"id":"A-1"},{"id":"A-2"}]}'
    )


async def test_a_pointer_indexes_into_an_array():
    _is_error, run = await _run(
        _server(),
        [
            {"id": "orders", "tool": "list_orders", "arguments": {"customer_id": 42}},
            {
                "tool": "refund_order",
                "arguments": {"order_id": {"$from": "orders", "path": "/orders/1/id"}},
            },
        ],
    )
    assert orjson.loads(run["steps"][1]["result"]["content"][0]["text"]) == {"refunded": "A-2"}


async def test_a_reference_without_a_path_takes_the_whole_result():
    _is_error, run = await _run(
        _server(),
        [
            {"id": "cust", "tool": "find_customer", "arguments": {"email": "a@b.c"}},
            {
                "tool": "refund_order",
                "arguments": {"order_id": {"$from": "cust", "path": "/email"}},
            },
        ],
    )
    assert orjson.loads(run["steps"][1]["result"]["content"][0]["text"])["refunded"] == "a@b.c"


async def test_a_quiet_step_is_left_out_of_the_response():
    """What makes the chain cheap: the intermediate never reaches the model."""
    _is_error, run = await _run(
        _server(),
        [
            {"id": "cust", "tool": "find_customer", "arguments": {"email": "a@b.c"}, "quiet": True},
            {
                "tool": "list_orders",
                "arguments": {"customer_id": {"$from": "cust", "path": "/id"}},
            },
        ],
    )
    assert [step["tool"] for step in run["steps"]] == ["list_orders"]


async def test_a_quiet_step_that_fails_is_still_reported():
    """Silence about a failure would leave the model guessing."""
    _is_error, run = await _run(
        _server(), [{"tool": "find_customer", "arguments": {}, "quiet": True}]
    )
    assert run["steps"][0]["result"]["isError"] is True


# ── When a plan goes wrong ───────────────────────────────────────────


async def test_the_run_stops_at_the_first_failure():
    _is_error, run = await _run(
        _server(),
        [
            {"tool": "find_customer", "arguments": {}},
            {"tool": "refund_order", "arguments": {"order_id": "A-1"}},
        ],
    )
    assert run["stopped"] == "step1"
    assert len(run["steps"]) == 1


async def test_the_run_can_be_told_to_carry_on():
    _is_error, run = await _run(
        _server(),
        [
            {"tool": "find_customer", "arguments": {}},
            {"tool": "refund_order", "arguments": {"order_id": "A-1"}},
        ],
        stop_on_error=False,
    )
    assert "stopped" not in run
    assert len(run["steps"]) == 2


async def test_a_reference_to_a_step_that_did_not_run_names_it():
    is_error, text = await _run(
        _server(), [{"tool": "refund_order", "arguments": {"order_id": {"$from": "nope"}}}]
    )
    assert is_error is True
    assert "nope" in text


async def test_a_pointer_that_names_nothing_says_so():
    is_error, text = await _run(
        _server(),
        [
            {"id": "cust", "tool": "find_customer", "arguments": {"email": "a@b.c"}},
            {
                "tool": "refund_order",
                "arguments": {"order_id": {"$from": "cust", "path": "/missing"}},
            },
        ],
    )
    assert is_error is True
    assert "/missing" in text


async def test_a_pointer_past_the_end_of_an_array_says_so():
    is_error, text = await _run(
        _server(),
        [
            {"id": "orders", "tool": "list_orders", "arguments": {"customer_id": 1}},
            {
                "tool": "refund_order",
                "arguments": {"order_id": {"$from": "orders", "path": "/orders/9/id"}},
            },
        ],
    )
    assert is_error is True
    assert "past the end" in text


async def test_a_plan_cannot_run_the_plan_runner():
    is_error, text = await _run(_server(), [{"tool": "run_tools", "arguments": {"steps": []}}])
    assert is_error is True
    assert "run_tools" in text


async def test_a_step_the_caller_may_not_invoke_is_not_invoked():
    """A plan is not a way around the scopes a direct call is held to."""
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    _is_error, run = await _run(_server(), [{"tool": "wipe", "arguments": {}}])
    assert run["steps"][0]["result"]["isError"] is True


# ── Registering it ───────────────────────────────────────────────────


def test_a_name_collision_says_which_name_and_what_to_do():
    app = Veloce(title="Clash", openapi_url=None)

    @app.mcp_tool(name="search_tools", description="Something else entirely")
    async def mine(q: str) -> dict:
        return {}

    with pytest.raises(ValueError, match="search_tools"):
        MCPServer(app, tool_search=True)


async def test_a_server_with_no_tools_at_all_still_answers():
    empty = Veloce(title="Empty", openapi_url=None)
    server = MCPServer(empty, tool_search=True)
    _is_error, found = await _call(server, "search_tools", {"query": "anything"})
    assert found == []
