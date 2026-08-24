"""One answer to "which protocol era is this message being answered in".

The era was re-derived at each site that shapes a result, from a `modern=`
keyword the caller had to remember to pass. Three sites forgot: the `tool_search`
catalogue advertised the handshake tool shape to a modern client, and
`tasks/cancel` and the task status notification spelled the duration fields the
handshake way after creation and polling had spelled them the modern way.

`handle_message` now resolves the era once and publishes it, and every shaping
function reads it. The sites that were wrong become right without being changed,
which is the property this file pins.
"""

from __future__ import annotations

import asyncio
import contextlib

import orjson

from veloce import Veloce
from veloce.contrib.mcp._helpers import _notifier_var
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.tasks import TASKS_EXTENSION

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}},
}


def _params(extra: dict | None = None, *, modern: bool) -> dict:
    params = dict(extra or {})
    if modern:
        params["_meta"] = MODERN_META
    return params


def _msg(method: str, extra: dict | None = None, *, modern: bool, mid: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": _params(extra, modern=modern)}


# ── The catalogue door agrees with the listing door ──────────────────


def _search_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Runs in the background", task_support=True)
    async def slow() -> dict:
        return {"ok": True}

    return app


async def _described(server: MCPServer, *, modern: bool) -> dict:
    """The entry `describe_tools` advertises - the only one a search server shows."""
    response = await server.handle_message(
        _msg(
            "tools/call",
            {"name": "describe_tools", "arguments": {"names": ["slow"]}},
            modern=modern,
        ),
        MCPSession(),
    )
    entries = orjson.loads(response["result"]["content"][0]["text"])
    return entries[0]


async def _listed(server: MCPServer, *, modern: bool) -> dict:
    response = await server.handle_message(_msg("tools/list", modern=modern), MCPSession())
    return {tool["name"]: tool for tool in response["result"]["tools"]}["slow"]


async def test_the_catalogue_omits_execution_for_a_modern_client():
    """The defect: `describe_tools` advertised the handshake shape era-blind."""
    entry = await _described(MCPServer(_search_app(), tool_search=True), modern=True)
    assert "execution" not in entry


async def test_the_catalogue_still_carries_execution_for_a_handshake_client():
    entry = await _described(MCPServer(_search_app(), tool_search=True), modern=False)
    assert entry["execution"] == {"taskSupport": "optional"}


async def test_both_tool_definition_doors_agree():
    """A search server shows only the catalogue, so the two must not diverge."""
    for modern in (False, True):
        server = MCPServer(_search_app(), tool_search=True)
        described = await _described(server, modern=modern)
        listed = await _listed(MCPServer(_search_app()), modern=modern)
        assert ("execution" in described) == ("execution" in listed), modern


# ── Every task-shaping site agrees on the field names ────────────────


def _task_app() -> tuple[Veloce, asyncio.Event]:
    app = Veloce(openapi_url=None)
    gate = asyncio.Event()

    @app.mcp_tool(description="Blocks until released", task_support=True)
    async def blocker() -> int:
        await gate.wait()
        return 1

    return app, gate


async def _settle(server: MCPServer) -> None:
    for runner in [t.runner for t in server._tasks.tasks.values() if t.runner is not None]:
        with contextlib.suppress(asyncio.CancelledError):
            await runner


async def test_a_modern_client_reads_the_same_duration_field_at_every_stage():
    """The defect: `ttlMs` on creation and polling, `ttl` on cancellation."""
    app, _gate = _task_app()
    server = MCPServer(app)
    session = MCPSession()

    created = await server.handle_message(
        _msg("tools/call", {"name": "blocker", "arguments": {}, "task": {}}, modern=True), session
    )
    task_id = created["result"]["task"]["taskId"]

    got = await server.handle_message(
        _msg("tasks/get", {"taskId": task_id}, modern=True, mid=2), session
    )
    cancelled = await server.handle_message(
        _msg("tasks/cancel", {"taskId": task_id}, modern=True, mid=3), session
    )
    await _settle(server)

    for stage, payload in (
        ("create", created["result"]["task"]),
        ("get", got["result"]),
        ("cancel", cancelled["result"]),
    ):
        assert "ttlMs" in payload, stage
        assert "ttl" not in payload, stage


async def test_a_handshake_client_reads_the_handshake_names_at_every_stage():
    app, _gate = _task_app()
    server = MCPServer(app)
    session = MCPSession()

    created = await server.handle_message(
        _msg("tools/call", {"name": "blocker", "arguments": {}, "task": {}}, modern=False), session
    )
    task_id = created["result"]["task"]["taskId"]
    cancelled = await server.handle_message(
        _msg("tasks/cancel", {"taskId": task_id}, modern=False, mid=3), session
    )
    await _settle(server)

    for stage, payload in (("create", created["result"]["task"]), ("cancel", cancelled["result"])):
        assert "ttl" in payload, stage
        assert "ttlMs" not in payload, stage


async def test_a_status_notification_carries_the_callers_era():
    """A notification is emitted from the task runner, not from the request."""
    app, gate = _task_app()
    server = MCPServer(app)
    session = MCPSession()
    seen: list[dict] = []

    async def notifier(message: dict) -> None:
        seen.append(message)

    token = _notifier_var.set(notifier)
    try:
        await server.handle_message(
            _msg("tools/call", {"name": "blocker", "arguments": {}, "task": {}}, modern=True),
            session,
        )
        gate.set()
        await _settle(server)
    finally:
        _notifier_var.reset(token)

    statuses = [m for m in seen if m.get("method") == "notifications/tasks/status"]
    assert statuses, "the runner emitted no status notification"
    for message in statuses:
        assert "ttlMs" in message["params"]["task"]
        assert "ttl" not in message["params"]["task"]


# ── The era resolves outside a transport too ─────────────────────────


async def test_a_bare_dispatch_defaults_to_the_handshake_era():
    """`handle_message` is callable with no transport and no session."""
    entry = await _listed(MCPServer(_search_app()), modern=False)
    assert entry["execution"] == {"taskSupport": "optional"}


async def test_two_eras_are_served_on_one_server_without_leaking():
    """The era is per message: one connection may carry both."""
    server = MCPServer(_search_app())
    assert "execution" in await _listed(server, modern=False)
    assert "execution" not in await _listed(server, modern=True)
    assert "execution" in await _listed(server, modern=False)
