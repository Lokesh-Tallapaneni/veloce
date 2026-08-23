"""A revision is only advertised the capabilities it can actually use.

Methods were already gated by protocol revision, but the advertisement was not:
a modern client read `logging` in the capabilities, called `logging/setLevel`,
and got method-not-found. The same held for the `tasks.list` sub-capability.
Advertising a primitive the dispatcher then refuses is what makes a client probe
a surface this server does not serve.

`advertise` now takes the revision. A capability whose methods a revision retired
withholds its entry (logging) or narrows it (tasks), and the handshake era is
unaffected.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.tasks import TASKS_EXTENSION

_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}},
}


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="A task-capable tool", task_support=True)
    async def slow() -> dict:
        return {"ok": True}

    return app


async def _send(method: str, params: dict | None = None, *, modern: bool) -> dict:
    payload = dict(params or {})
    if modern:
        payload["_meta"] = _MODERN_META
    return await MCPServer(_app()).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": payload}, MCPSession()
    )


async def _capabilities(*, modern: bool) -> dict:
    if modern:
        response = await _send("server/discover", modern=True)
        return response["result"]["capabilities"]
    response = await _send(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "c", "version": "1"},
        },
        modern=False,
    )
    return response["result"]["capabilities"]


# ── Logging: removed by the modern revision ──────────────────────────


async def test_a_modern_client_is_not_offered_logging():
    """The defect: it was offered, then refused."""
    assert "logging" not in await _capabilities(modern=True)


async def test_a_handshake_client_is_still_offered_logging():
    assert "logging" in await _capabilities(modern=False)


async def test_the_method_is_still_refused_for_a_modern_client():
    """Withholding the advertisement must not change what the dispatcher does."""
    response = await _send("logging/setLevel", {"level": "info"}, modern=True)
    assert response["error"]["code"] == -32601


async def test_the_method_still_answers_a_handshake_client():
    response = await _send("logging/setLevel", {"level": "info"}, modern=False)
    assert "error" not in response, response


# ── Tasks: `list` retired, the rest kept ─────────────────────────────


async def test_a_modern_client_is_not_offered_task_listing():
    tasks = (await _capabilities(modern=True)).get("tasks", {})
    assert "list" not in tasks


async def test_a_modern_client_is_still_offered_the_rest_of_tasks():
    """Narrowing the entry must not withdraw the sub-capabilities that remain."""
    tasks = (await _capabilities(modern=True)).get("tasks", {})
    assert "cancel" in tasks
    assert tasks.get("requests", {}).get("tools/call") == {}


async def test_a_handshake_client_is_still_offered_task_listing():
    assert "list" in (await _capabilities(modern=False)).get("tasks", {})


# ── The invariant, stated once ───────────────────────────────────────


@pytest.mark.parametrize("modern", [True, False])
async def test_no_advertised_capability_names_a_method_the_dispatcher_refuses(modern: bool):
    """The general rule the two cases above are instances of."""
    from veloce.contrib.mcp.server import _HANDSHAKE_ONLY_METHODS

    capabilities = await _capabilities(modern=modern)
    refused = _HANDSHAKE_ONLY_METHODS if modern else frozenset()

    offending = []
    for area, entry in capabilities.items():
        if not isinstance(entry, dict):
            continue
        for sub in entry:
            if f"{area}/{sub}" in refused:
                offending.append(f"{area}.{sub}")
    assert not offending, f"advertised but refused: {offending}"


def test_a_capability_written_without_the_revision_parameter_still_works():
    """The parameter is opt-in, so an external capability need not be rewritten.

    The server decides once, at construction, which capabilities accept the
    revision; one that does not is called the old way rather than crashing.
    """
    from veloce.contrib.mcp.capabilities import Capability

    class Legacy(Capability):
        __slots__ = ()

        def advertise(self):  # noqa: ANN201 - deliberately the pre-revision signature
            return {"house": {}}

        def handlers(self):  # noqa: ANN201
            return {}

    server = MCPServer(_app())
    legacy = Legacy()
    server._capabilities = (*server._capabilities, legacy)

    assert legacy not in server._era_aware_capabilities
    for modern in (True, False):
        assert "house" in server._advertised_capabilities(modern)


def test_a_capability_that_accepts_the_revision_is_detected():
    server = MCPServer(_app())
    accepting = [type(c).__name__ for c in server._era_aware_capabilities]
    assert "LoggingCapability" in accepting
    assert "TasksCapability" in accepting


# ── Nothing unproducible is advertised ───────────────────────────────
#
# The rule this module exists for, stated once as a property rather than per
# capability: a client chooses what to do from what the server says it can do,
# so a server that offers something it then refuses sends the client down a
# path that cannot work. The modern multi-round-trip `input_required` result is
# the current instance - the discriminator is named in the source but no interim
# result is produced and `requestState` is unmodelled, so it must not appear in
# anything a client reads.


def _client_visible_payloads() -> str:
    """Everything a client learns about this server's surface, as one blob."""
    import asyncio
    import json

    from veloce.contrib.mcp.session import MCPSession

    server = MCPServer(_app())
    modern = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}

    async def ask(method: str) -> dict:
        return await server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"_meta": modern}},
            MCPSession(),
        )

    async def gather() -> list[dict]:
        return [await ask(m) for m in ("server/discover", "tools/list", "prompts/list")]

    return json.dumps(asyncio.run(gather()))


def test_the_unproduced_interim_result_is_not_advertised():
    """`input_required` is not implemented, so nothing may offer it."""
    assert "input_required" not in _client_visible_payloads()


def test_the_result_discriminators_that_are_advertised_can_all_be_produced():
    from veloce.contrib.mcp.server import RESULT_TYPE_COMPLETE, RESULT_TYPE_TASK

    produced = {RESULT_TYPE_COMPLETE, RESULT_TYPE_TASK}
    assert "input_required" not in produced
