"""Which methods a modern client loses is declared once, by the capability.

The rule lived in three files. A hardcoded `_HANDSHAKE_ONLY_METHODS` frozenset in
the dispatcher decided what to *refuse*; `LoggingCapability.advertise(modern=)`
and `TasksCapability.advertise(modern=)` separately decided what to *withhold*.

All three agreed, so nothing was broken. But `capabilities/base.py` is explicit
that "A new spec area is a new capability registered on the server, not a new
branch in a dispatcher", and this one rule sat half in a capability and half in a
name table somewhere else. A capability author adding an era-retired method edits
the half they can see, and the server then advertises a method it refuses — the
exact failure the two halves exist to prevent.

Each capability declares `handshake_only_methods` beside the advertisement that
withholds them, and the server unions them the same way it merges `handlers()`.
`ping` stays in the server, because it belongs to no capability.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp.capabilities.base import Capability
from veloce.contrib.mcp.server import (
    _CORE_HANDSHAKE_ONLY_METHODS,
    MODERN_PROTOCOL_VERSION,
    MCPServer,
)

#: What the old hardcoded frozenset contained. The union must still equal it.
HISTORIC_SET = frozenset({"logging/setLevel", "ping", "tasks/list", "tasks/result"})


def _app() -> Veloce:
    app = Veloce(title="EraOwnership", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    return app


def _server() -> MCPServer:
    return MCPServer(_app())


# ── the union is what it always was ──────────────────────────────────


def test_the_derived_set_matches_the_hardcoded_one():
    """A refactor of where a rule lives must not change the rule."""
    assert _server()._handshake_only == HISTORIC_SET


def test_ping_comes_from_the_server():
    """It is answered by the server itself, so no capability can own it."""
    assert frozenset({"ping"}) == _CORE_HANDSHAKE_ONLY_METHODS


def test_the_capabilities_supply_the_rest():
    server = _server()
    from_capabilities = frozenset().union(
        *(capability.handshake_only_methods for capability in server._capabilities)
    )
    assert from_capabilities == HISTORIC_SET - _CORE_HANDSHAKE_ONLY_METHODS


def test_the_dispatcher_holds_no_second_table():
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "veloce"
        / "contrib"
        / "mcp"
        / "server.py"
    ).read_text(encoding="utf-8")
    assert "_HANDSHAKE_ONLY_METHODS = frozenset(\n    {\n" not in source
    assert "self._handshake_only" in source


# ── the contract every capability inherits ───────────────────────────


def test_the_base_declares_an_empty_default():
    """A capability that retires nothing says nothing."""
    assert Capability.handshake_only_methods == frozenset()


def test_every_capability_declares_a_frozenset():
    for capability in _server()._capabilities:
        assert isinstance(capability.handshake_only_methods, frozenset)


def test_a_capability_only_retires_methods_it_answers():
    """Retiring a method you do not handle is a rule with no effect."""
    for capability in _server()._capabilities:
        assert capability.handshake_only_methods <= set(capability.handlers())


def test_every_retired_method_is_actually_registered():
    """Otherwise the refusal is dead code and the rule is a comment."""
    server = _server()
    for method in server._handshake_only - _CORE_HANDSHAKE_ONLY_METHODS:
        assert method in server._methods


# ── the two halves of the rule agree ─────────────────────────────────


def test_a_capability_does_not_advertise_what_it_retires():
    """The property the split made possible to get wrong."""
    import json

    server = _server()
    for capability in server._capabilities:
        if not capability.handshake_only_methods:
            continue
        try:
            entry = capability.advertise(modern=True)
        except TypeError:  # pragma: no cover - not era-aware
            continue
        rendered = json.dumps(entry or {})
        for method in capability.handshake_only_methods:
            # The advertisement names the sub-capability, not the full method.
            assert method.split("/")[-1] not in json.loads(rendered)


def test_logging_is_withheld_on_the_modern_revision():
    from veloce.contrib.mcp.capabilities.concrete import LoggingCapability

    capability = next(c for c in _server()._capabilities if isinstance(c, LoggingCapability))
    assert capability.advertise(modern=True) is None
    assert capability.advertise(modern=False) == {"logging": {}}


def test_tasks_withholds_list_on_the_modern_revision():
    from veloce.contrib.mcp.tasks import TasksCapability

    app = Veloce(title="Tasked", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="A long job", task_support=True)
    async def job() -> dict:
        return {}

    capability = next(c for c in MCPServer(app)._capabilities if isinstance(c, TasksCapability))
    assert "list" not in capability.advertise(modern=True)["tasks"]
    assert "list" in capability.advertise(modern=False)["tasks"]


# ── the dispatcher still refuses them ────────────────────────────────


def _post(client, message, headers=None):
    return client.post(
        "/mcp", json=message, headers={"Accept": "application/json", **(headers or {})}
    )


def _modern(method: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION}},
    }


def _mounted() -> Veloce:
    app = _app()
    app.mount_mcp(transport="http", path="/mcp")
    return app


@pytest.mark.parametrize("method", ["ping", "logging/setLevel"])
def test_a_retired_method_is_not_found_on_the_modern_revision(method):
    client = _mounted().test_client()
    response = _post(
        client,
        _modern(method),
        {"MCP-Protocol-Version": MODERN_PROTOCOL_VERSION, "Mcp-Method": method},
    )
    assert response.json()["error"]["code"] == -32601


@pytest.mark.parametrize("method", ["ping", "logging/setLevel"])
def test_a_retired_method_still_works_on_the_handshake_revision(method):
    client = _mounted().test_client()
    _post(
        client,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
    )
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if method == "logging/setLevel":
        body["params"] = {"level": "info"}
    assert "error" not in _post(client, body).json()


def test_a_method_no_capability_retires_is_served_on_both():
    client = _mounted().test_client()
    modern = _post(
        client,
        _modern("tools/list"),
        {"MCP-Protocol-Version": MODERN_PROTOCOL_VERSION, "Mcp-Method": "tools/list"},
    )
    assert "error" not in modern.json()


# ── a new capability's declaration is honoured ───────────────────────


def test_a_capability_retiring_a_method_has_it_refused():
    """The whole point: one edit, and the dispatcher follows."""
    from veloce.contrib.mcp.capabilities.base import _ServerCapability

    class Extra(_ServerCapability):
        __slots__ = ()
        handshake_only_methods = frozenset({"extra/legacy"})

        def advertise(self, *, modern: bool = False):
            return None if modern else {"extra": {}}

        def handlers(self):
            async def legacy(*args, **kwargs):
                return {}

            return {"extra/legacy": legacy}

    rebuilt = MCPServer(_app())
    rebuilt._capabilities = (*rebuilt._capabilities, Extra(rebuilt))
    rebuilt._handshake_only = _CORE_HANDSHAKE_ONLY_METHODS.union(
        *(c.handshake_only_methods for c in rebuilt._capabilities)
    )
    assert "extra/legacy" in rebuilt._handshake_only


def test_a_capability_retiring_nothing_adds_nothing():
    from veloce.contrib.mcp.capabilities.base import _ServerCapability

    class Quiet(_ServerCapability):
        __slots__ = ()

        def advertise(self, *, modern: bool = False):
            return {"quiet": {}}

        def handlers(self):
            return {}

    assert Quiet.handshake_only_methods == frozenset()
