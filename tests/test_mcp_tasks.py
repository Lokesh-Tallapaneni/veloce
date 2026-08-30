"""MCP tasks - task-augmented tool calls, lifecycle, and the task methods."""

from __future__ import annotations

import asyncio

import pytest

from tests._mcp import INVALID_PARAMS, RESOURCE_NOT_FOUND, await_tasks, call
from veloce import Veloce
from veloce.contrib.mcp import MCPTask, TaskRegistry, TasksCapability
from veloce.contrib.mcp.server import MCPServer, _notifier_var
from veloce.contrib.mcp.tasks import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_WORKING,
    TASK_STATUSES,
)


def _server(app: Veloce) -> MCPServer:
    return MCPServer(app)


async def _drive(server: MCPServer, message: dict) -> dict | None:
    """Dispatch one message through the server (no transport session)."""
    return await server.handle_message(message)


def _call(name: str, arguments: dict, *, task: bool = False) -> dict:
    params: dict = {"name": name, "arguments": arguments}
    if task:
        params["task"] = {}
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}


# -- Opt-in advertisement ----------------------------------------------


async def test_tasks_capability_silent_without_opt_in():
    """A server whose tools do not opt in advertises no tasks capability."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    server = _server(app)
    assert TasksCapability(server).advertise() is None
    init = await call(server, "initialize", {})
    assert "tasks" not in init["capabilities"]


async def test_tasks_capability_advertised_when_a_tool_opts_in():
    """One task-supporting tool turns the tasks capability on."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Slow add", task_support=True)
    async def slow_add(a: int, b: int) -> int:
        return a + b

    server = _server(app)
    advert = TasksCapability(server).advertise()
    assert advert == {"tasks": {"list": {}, "cancel": {}, "requests": {"tools/call": {}}}}
    assert (await call(server, "initialize", {}))["capabilities"]["tasks"] == advert["tasks"]


async def test_execution_task_support_on_tools_list():
    """tools/list advertises execution.taskSupport only for an opted-in tool."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Slow", task_support=True)
    async def slow() -> int:
        return 1

    @app.mcp_tool(description="Fast")
    async def fast() -> int:
        return 2

    server = _server(app)
    listed = await call(server, "tools/list")
    by_name = {t["name"]: t for t in listed["tools"]}
    assert by_name["slow"]["execution"] == {"taskSupport": "optional"}
    assert "execution" not in by_name["fast"]


# -- Task creation + result --------------------------------------------


def test_task_augmented_call_returns_create_task_result():
    """A task-augmented call returns a CreateTaskResult, not the tool result."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Slow add", task_support=True)
    async def slow_add(a: int, b: int) -> int:
        return a + b

    server = _server(app)

    async def run() -> dict:
        resp = await _drive(server, _call("slow_add", {"a": 2, "b": 3}, task=True))
        await await_tasks(server)
        return resp

    resp = asyncio.run(run())
    result = resp["result"]
    task = result["task"]
    assert task["taskId"] in server._tasks.tasks
    assert task["status"] == STATUS_WORKING
    assert result["_meta"]["io.modelcontextprotocol/model-immediate-response"] is True


def test_tasks_result_returns_the_completed_tool_result():
    """tasks/result yields the same result a synchronous call would produce."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Slow add", task_support=True)
    async def slow_add(a: int, b: int) -> int:
        return a + b

    server = _server(app)

    async def run() -> tuple[dict, dict]:
        created = await _drive(server, _call("slow_add", {"a": 2, "b": 3}, task=True))
        await await_tasks(server)
        task_id = created["result"]["task"]["taskId"]
        got = await _drive(
            server,
            {"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"taskId": task_id}},
        )
        result = await _drive(
            server,
            {"jsonrpc": "2.0", "id": 3, "method": "tasks/result", "params": {"taskId": task_id}},
        )
        return got, result

    got, result = asyncio.run(run())
    assert got["result"]["status"] == STATUS_COMPLETED
    assert result["result"]["content"][0]["text"] == "5"


def test_tasks_result_while_working_is_invalid_params():
    """tasks/result before the task settles is an invalid-params error."""
    app = Veloce(openapi_url=None)

    gate = asyncio.Event()

    @app.mcp_tool(description="Blocks", task_support=True)
    async def blocker() -> int:
        await gate.wait()
        return 1

    server = _server(app)

    async def run() -> dict:
        created = await _drive(server, _call("blocker", {}, task=True))
        task_id = created["result"]["task"]["taskId"]
        early = await _drive(
            server,
            {"jsonrpc": "2.0", "id": 2, "method": "tasks/result", "params": {"taskId": task_id}},
        )
        gate.set()
        await await_tasks(server)
        return early

    early = asyncio.run(run())
    assert early["error"]["code"] == INVALID_PARAMS


# -- Failure + lifecycle -----------------------------------------------


def test_failing_handler_settles_task_failed():
    """A handler that raises settles its task as failed with an isError result."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Boom", task_support=True)
    async def boom() -> int:
        raise RuntimeError("kaput")

    server = _server(app)

    async def run() -> dict:
        created = await _drive(server, _call("boom", {}, task=True))
        await await_tasks(server)
        task_id = created["result"]["task"]["taskId"]
        return await _drive(
            server,
            {"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"taskId": task_id}},
        )

    got = asyncio.run(run())
    assert got["result"]["status"] == STATUS_FAILED


def test_tasks_cancel_moves_task_to_cancelled():
    """tasks/cancel unwinds a running task and reports cancelled status."""
    app = Veloce(openapi_url=None)

    gate = asyncio.Event()

    @app.mcp_tool(description="Blocks", task_support=True)
    async def blocker() -> int:
        await gate.wait()
        return 1

    server = _server(app)

    async def run() -> dict:
        created = await _drive(server, _call("blocker", {}, task=True))
        task_id = created["result"]["task"]["taskId"]
        cancelled = await _drive(
            server,
            {"jsonrpc": "2.0", "id": 2, "method": "tasks/cancel", "params": {"taskId": task_id}},
        )
        await await_tasks(server)
        return cancelled

    cancelled = asyncio.run(run())
    assert cancelled["result"]["status"] == STATUS_CANCELLED


def test_tasks_cancel_emits_a_status_notification():
    """tasks/cancel awaits the cancelled status notification so it is not dropped."""
    app = Veloce(openapi_url=None)

    gate = asyncio.Event()

    @app.mcp_tool(description="Blocks", task_support=True)
    async def blocker() -> int:
        await gate.wait()
        return 1

    server = _server(app)
    sent: list[dict] = []

    async def sink(message: dict) -> None:
        sent.append(message)

    async def run() -> None:
        _notifier_var.set(sink)
        created = await _drive(server, _call("blocker", {}, task=True))
        task_id = created["result"]["task"]["taskId"]
        await _drive(
            server,
            {"jsonrpc": "2.0", "id": 2, "method": "tasks/cancel", "params": {"taskId": task_id}},
        )
        await await_tasks(server)

    asyncio.run(run())
    status_msgs = [m for m in sent if m.get("method") == "notifications/tasks/status"]
    assert any(m["params"]["task"]["status"] == STATUS_CANCELLED for m in status_msgs)


def test_tasks_list_reports_known_tasks():
    """tasks/list enumerates the created tasks."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add", task_support=True)
    async def add(a: int, b: int) -> int:
        return a + b

    server = _server(app)

    async def run() -> dict:
        await _drive(server, _call("add", {"a": 1, "b": 1}, task=True))
        await await_tasks(server)
        return await _drive(
            server, {"jsonrpc": "2.0", "id": 9, "method": "tasks/list", "params": {}}
        )

    listing = asyncio.run(run())
    assert len(listing["result"]["tasks"]) == 1


def test_unknown_task_id_is_resource_not_found():
    """A task method naming an unknown id is a resource-not-found error."""
    server = _server(Veloce(openapi_url=None))
    resp = asyncio.run(
        _drive(
            server, {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"taskId": "x"}}
        )
    )
    assert resp["error"]["code"] == RESOURCE_NOT_FOUND


# -- Opt-out rejection -------------------------------------------------


def test_task_call_on_non_opting_tool_is_rejected():
    """A task-augmented call to a tool that did not opt in is invalid-params."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    server = _server(app)
    resp = asyncio.run(_drive(server, _call("add", {"a": 1, "b": 2}, task=True)))
    assert resp["error"]["code"] == INVALID_PARAMS


# -- Dual door: a route runs the same handler synchronously and as a task ----


def test_route_runs_one_handler_through_both_doors():
    """A route-backed tool yields the same result synchronously and as a task."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/double/{n}",
        expose_as_mcp_tool=True,
        mcp_description="Double a number",
        mcp_task_support=True,
    )
    async def double(n: int) -> dict:
        return {"result": n * 2}

    server = _server(app)

    async def run() -> tuple[dict, dict]:
        sync = await _drive(server, _call("double", {"n": 5}))
        created = await _drive(server, _call("double", {"n": 5}, task=True))
        await await_tasks(server)
        task_id = created["result"]["task"]["taskId"]
        task_result = await _drive(
            server,
            {"jsonrpc": "2.0", "id": 2, "method": "tasks/result", "params": {"taskId": task_id}},
        )
        return sync, task_result

    sync, task_result = asyncio.run(run())
    sync_text = sync["result"]["content"][0]["text"]
    task_text = task_result["result"]["content"][0]["text"]
    assert sync_text == task_text
    assert "10" in sync_text


# -- Notifications ------------------------------------------------------


def test_status_notification_carries_related_task_meta():
    """A settling task emits notifications/tasks/status with the related-task meta."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add", task_support=True)
    async def add(a: int, b: int) -> int:
        return a + b

    server = _server(app)
    sent: list[dict] = []

    async def sink(message: dict) -> None:
        sent.append(message)

    async def run() -> None:
        _notifier_var.set(sink)
        await _drive(server, _call("add", {"a": 1, "b": 2}, task=True))
        await await_tasks(server)

    asyncio.run(run())
    status_msgs = [m for m in sent if m.get("method") == "notifications/tasks/status"]
    assert status_msgs
    meta = status_msgs[-1]["params"]["_meta"]["io.modelcontextprotocol/related-task"]
    assert "taskId" in meta


# -- Registry + descriptor ---------------------------------------------


def test_task_registry_is_a_registry_of_mcptask():
    """The task store reuses the generic Registry base, rejecting duplicate ids."""
    registry = TaskRegistry()
    task = MCPTask(name="abc", description="")
    registry.register(task)
    assert registry.get("abc") is task
    with pytest.raises(ValueError, match="Duplicate MCP task id"):
        registry.register(MCPTask(name="abc", description=""))


def test_task_status_set_models_the_full_lifecycle():
    """The modelled status set is the spec's five-state lifecycle."""
    assert {
        "working",
        "input_required",
        "completed",
        "failed",
        "cancelled",
    } == TASK_STATUSES
