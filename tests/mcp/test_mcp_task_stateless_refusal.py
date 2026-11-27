"""A task-augmented call is refused when the connection has no session.

`mount_mcp` already refuses `sessions=False` when a registered tool declares
`task_support=True`, because a throwaway per-POST session makes the resulting
task unreachable: `tasks/*` from a later request presents a different connection
id, and `TaskRegistry.evict_expired` deliberately drops only *settled* tasks. A
never-settling task would therefore be pinned for the process lifetime with
nobody able to poll or cancel it.

That guard reads the registry as it stands at mount time, so declaring the tool
afterwards walked straight past it. The refusal below is on the call, which no
registration order can defeat.
"""

from __future__ import annotations

import pytest

from tests._mcp import INVALID_PARAMS, live_tasks
from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


def _app() -> Veloce:
    app = Veloce(title="Tasks", openapi_url=None)

    @app.mcp_tool(description="Runs as a task")
    async def slow() -> str:
        return "done"

    return app


async def _call(app: Veloce, session: MCPSession) -> dict:
    return await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {}, "task": {"ttl": 60000}},
        },
        session,
    )


def _task_app() -> Veloce:
    app = Veloce(title="Tasks", openapi_url=None)

    @app.mcp_tool(description="Runs as a task", task_support=True)
    async def slow() -> str:
        return "done"

    return app


# ── The mount-time guard still stands ────────────────────────────────


def test_mounting_stateless_with_a_task_tool_is_refused():
    app = _task_app()
    with pytest.raises(ValueError, match="sessions=True"):
        app.mount_mcp(transport="http", path="/mcp")


def test_mounting_stateless_without_a_task_tool_is_allowed():
    """The guard must not refuse an app that has no task tools at all."""
    _app().mount_mcp(transport="http", path="/mcp")


# ── The call-time guard closes the ordering hole ─────────────────────


def test_a_task_tool_can_be_registered_after_a_stateless_mount():
    """Registration order is not itself an error - the call is what is refused."""
    app = _app()
    app.mount_mcp(transport="http", path="/mcp")

    @app.mcp_tool(description="Declared after the mount", task_support=True)
    async def late() -> str:
        return "done"


async def test_a_task_call_on_a_non_persistent_session_is_refused():
    """The retention vector: this used to be accepted and pinned forever."""
    session = MCPSession(persistent=False)
    response = await _call(_task_app(), session)
    assert "result" not in response
    assert response["error"]["code"] == INVALID_PARAMS
    assert "sessions=True" in response["error"]["message"]


async def test_the_refusal_registers_no_task():
    """A refused call must not leave anything behind to leak."""
    app = _task_app()
    server = MCPServer(app)
    await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {}, "task": {"ttl": 60000}},
        },
        MCPSession(persistent=False),
    )
    assert live_tasks(server) == {}


async def test_a_persistent_session_still_creates_the_task():
    """The guard must refuse only the unreachable case."""
    response = await _call(_task_app(), MCPSession(persistent=True))
    assert "error" not in response
    assert response["result"]["task"]["taskId"]


async def test_a_plain_call_on_a_non_persistent_session_is_unaffected():
    """Only task augmentation is refused; ordinary calls are the common path."""
    response = await MCPServer(_task_app()).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {}},
        },
        MCPSession(persistent=False),
    )
    assert "error" not in response
    assert response["result"]["content"][0]["text"] == "done"
