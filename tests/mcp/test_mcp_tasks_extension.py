"""The tasks extension on the modern revision.

Tasks moved out of the core protocol into `io.modelcontextprotocol/tasks`, which
changes four things a client can observe: the server advertises the extension, it
refuses to hand a task to a client that did not declare it, the handle comes back as
its own result type, and two of the four task methods no longer exist. The handshake
revision keeps everything it had.
"""

from __future__ import annotations

import pytest

from tests._mcp import METHOD_NOT_FOUND, await_tasks, task_by_id
from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.tasks import TASKS_EXTENSION

MODERN_BASE = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
MODERN_WITH_TASKS = {
    **MODERN_BASE,
    "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}},
}


def _app(*, task_support: bool = True) -> Veloce:
    app = Veloce(title="TaskProbe", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="A long job", task_support=task_support)
    async def slow(value: int = 1) -> dict:
        return {"doubled": value * 2}

    @app.mcp_tool(description="An ordinary tool")
    async def quick() -> dict:
        return {"ok": True}

    return app


async def _send(server: MCPServer, method: str, params: dict, meta: dict, session=None) -> dict:
    return await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": meta}},
        session if session is not None else MCPSession(),
    )


async def _create(server: MCPServer, meta: dict, session=None) -> dict:
    return await _send(
        server,
        "tools/call",
        {"name": "slow", "arguments": {"value": 21}, "task": {}},
        meta,
        session,
    )


# ── Advertisement ────────────────────────────────────────────────────


async def test_discover_advertises_the_tasks_extension():
    response = await _send(MCPServer(_app()), "server/discover", {}, MODERN_BASE)
    extensions = response["result"]["capabilities"]["extensions"]
    assert TASKS_EXTENSION in extensions


async def test_a_server_with_no_task_capable_tool_advertises_no_extension():
    response = await _send(MCPServer(_app(task_support=False)), "server/discover", {}, MODERN_BASE)
    capabilities = response["result"]["capabilities"]
    assert TASKS_EXTENSION not in capabilities.get("extensions", {})


# ── A task is only handed to a client that asked for one ─────────────


async def test_a_client_that_declared_the_extension_receives_a_task():
    response = await _create(MCPServer(_app()), MODERN_WITH_TASKS)
    result = response["result"]
    assert result["resultType"] == "task"
    assert result["task"]["status"] == "working"
    assert result["task"]["taskId"]


async def test_a_client_that_did_not_declare_it_is_refused():
    """Never hand a handle to a client with no methods to resolve it."""
    response = await _create(MCPServer(_app()), MODERN_BASE)
    assert "error" in response
    assert TASKS_EXTENSION in response["error"]["message"]


async def test_the_refusal_names_the_way_forward():
    response = await _create(MCPServer(_app()), MODERN_BASE)
    assert "without a 'task' field" in response["error"]["message"]


async def test_a_handshake_client_still_receives_a_task_without_declaring():
    """The extension's rule is modern-only; the old revision had no `extensions`."""
    server = MCPServer(_app())
    session = MCPSession()
    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {"value": 21}, "task": {}},
        },
        session,
    )
    assert "error" not in response
    assert response["result"]["task"]["status"] == "working"


# ── Field naming differs by revision ─────────────────────────────────


async def test_a_modern_task_uses_the_extension_field_names():
    response = await _create(MCPServer(_app()), MODERN_WITH_TASKS)
    task = response["result"]["task"]
    assert "ttlMs" in task and "pollIntervalMs" in task
    assert "ttl" not in task and "pollInterval" not in task


async def test_a_handshake_task_keeps_the_names_its_revision_defined():
    server = MCPServer(_app())
    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {"value": 21}, "task": {}},
        },
        MCPSession(),
    )
    task = response["result"]["task"]
    assert "ttl" in task and "pollInterval" in task
    assert "ttlMs" not in task


# ── Retired methods ──────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["tasks/list", "tasks/result"])
async def test_a_retired_method_is_not_found_for_a_modern_client(method: str):
    response = await _send(MCPServer(_app()), method, {"taskId": "x"}, MODERN_WITH_TASKS)
    assert response["error"]["code"] == METHOD_NOT_FOUND


@pytest.mark.parametrize("method", ["tasks/list", "tasks/result"])
async def test_a_retired_method_still_serves_a_handshake_client(method: str):
    """Removing them from the old revision would break clients that have them."""
    server = MCPServer(_app())
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"taskId": "unknown"}},
        MCPSession(),
    )
    # Reached the handler rather than the method-not-found path. `tasks/list`
    # answers with a (possibly empty) list; `tasks/result` rejects the unknown id.
    assert "error" not in response or response["error"]["code"] != -32601


async def test_tasks_get_still_serves_a_modern_client():
    server = MCPServer(_app())
    session = MCPSession()
    created = await _create(server, MODERN_WITH_TASKS, session)
    task_id = created["result"]["task"]["taskId"]
    polled = await _send(server, "tasks/get", {"taskId": task_id}, MODERN_WITH_TASKS, session)
    assert polled["result"]["taskId"] == task_id


async def test_a_completed_task_carries_its_result_when_polled():
    """`tasks/result` is gone, so the result must arrive with the status."""
    server = MCPServer(_app())
    session = MCPSession()
    created = await _create(server, MODERN_WITH_TASKS, session)
    task_id = created["result"]["task"]["taskId"]
    await await_tasks(server)
    polled = await _send(server, "tasks/get", {"taskId": task_id}, MODERN_WITH_TASKS, session)
    assert polled["result"]["status"] == "completed"
    assert "42" in str(polled["result"]["result"])


# ── tasks/update ─────────────────────────────────────────────────────


async def test_tasks_update_acknowledges_with_an_empty_result():
    server = MCPServer(_app())
    session = MCPSession()
    created = await _create(server, MODERN_WITH_TASKS, session)
    task_id = created["result"]["task"]["taskId"]
    response = await _send(
        server,
        "tasks/update",
        {"taskId": task_id, "inputResponses": {"k": {"action": "accept"}}},
        MODERN_WITH_TASKS,
        session,
    )
    assert response["result"] == {"resultType": "complete"}


async def test_tasks_update_records_the_responses_on_the_task():
    server = MCPServer(_app())
    session = MCPSession()
    created = await _create(server, MODERN_WITH_TASKS, session)
    task_id = created["result"]["task"]["taskId"]
    await _send(
        server,
        "tasks/update",
        {"taskId": task_id, "inputResponses": {"who": {"action": "accept"}}},
        MODERN_WITH_TASKS,
        session,
    )
    task = task_by_id(server, task_id)
    assert task.input_responses["who"] == {"action": "accept"}


async def test_tasks_update_without_responses_is_still_acknowledged():
    server = MCPServer(_app())
    session = MCPSession()
    created = await _create(server, MODERN_WITH_TASKS, session)
    task_id = created["result"]["task"]["taskId"]
    response = await _send(server, "tasks/update", {"taskId": task_id}, MODERN_WITH_TASKS, session)
    assert "error" not in response


async def test_tasks_update_on_an_unknown_task_is_an_error():
    response = await _send(MCPServer(_app()), "tasks/update", {"taskId": "nope"}, MODERN_WITH_TASKS)
    assert "error" in response
