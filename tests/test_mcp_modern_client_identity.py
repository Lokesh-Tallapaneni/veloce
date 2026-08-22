"""Client identity on the modern revision, which has no `initialize`.

A modern client states who it is and what it supports in `_meta` on every request.
Nothing downstream should have to know which handshake produced that: `client_info`
and `client_capabilities` answer for both eras, and the capability gates on the
server-initiated requests read the same values.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.errors import MCPCapabilityError
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession

CLIENT_INFO = {"name": "claude-code", "title": "Claude Code", "version": "2.1.236"}
CLIENT_CAPS = {"roots": {"listChanged": True}, "elicitation": {}}
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
    "io.modelcontextprotocol/clientCapabilities": CLIENT_CAPS,
}


def _app() -> Veloce:
    app = Veloce(title="IdentityProbe", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Report the calling client")
    async def whoami(ctx: MCPContext) -> dict:
        return {
            "name": ctx.client_info.get("name"),
            "version": ctx.client_info.get("version"),
            "elicitation": ctx.client_supports("elicitation"),
            "sampling": ctx.client_supports("sampling"),
            "roots_list_changed": ctx.client_supports("roots.listChanged"),
        }

    return app


async def _modern_call(
    server: MCPServer,
    method: str,
    params: dict | None = None,
    session: MCPSession | None = None,
) -> dict:
    """Dispatch a modern request the way a transport does: with a session."""
    return await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {**(params or {}), "_meta": MODERN_META},
        },
        session if session is not None else MCPSession(),
    )


# ── Session recording ────────────────────────────────────────────────


def test_request_meta_records_identity_and_capabilities():
    session = MCPSession()
    session.record_request_meta(MODERN_META)
    assert session.client_info == CLIENT_INFO
    assert session.client_capabilities == CLIENT_CAPS


def test_absent_keys_leave_previous_values_alone():
    """A persistent session must not be blanked by a request that omits them."""
    session = MCPSession()
    session.record_request_meta(MODERN_META)
    session.record_request_meta({"io.modelcontextprotocol/protocolVersion": "2026-07-28"})
    assert session.client_info == CLIENT_INFO
    assert session.client_capabilities == CLIENT_CAPS


@pytest.mark.parametrize("meta", [None, "not-a-dict", 42, []])
def test_a_malformed_meta_is_ignored(meta):
    session = MCPSession()
    session.record_request_meta(meta)
    assert session.client_info is None


def test_malformed_members_are_ignored():
    session = MCPSession()
    session.record_request_meta(
        {
            "io.modelcontextprotocol/clientInfo": "nope",
            "io.modelcontextprotocol/clientCapabilities": ["nope"],
        }
    )
    assert session.client_info is None
    assert session.client_capabilities == {}


# ── End to end through a tool call ───────────────────────────────────


async def test_a_modern_tool_call_sees_the_client_identity():
    response = await _modern_call(
        MCPServer(_app()), "tools/call", {"name": "whoami", "arguments": {}}
    )
    text = response["result"]["content"][0]["text"]
    assert '"name":"claude-code"' in text
    assert '"version":"2.1.236"' in text


async def test_a_modern_tool_call_sees_the_advertised_capabilities():
    response = await _modern_call(
        MCPServer(_app()), "tools/call", {"name": "whoami", "arguments": {}}
    )
    text = response["result"]["content"][0]["text"]
    assert '"elicitation":true' in text
    assert '"sampling":false' in text
    assert '"roots_list_changed":true' in text


async def test_the_handshake_era_still_records_identity():
    """The `initialize` path must keep working unchanged."""
    server = MCPServer(_app())
    session = MCPSession()
    await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"sampling": {}},
                "clientInfo": {"name": "legacy-client", "version": "0.1"},
            },
        },
        session,
    )
    await server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        },
        session,
    )
    text = response["result"]["content"][0]["text"]
    assert '"name":"legacy-client"' in text
    assert '"sampling":true' in text


# ── The capability gates read the same values ────────────────────────


async def test_a_gate_accepts_a_capability_a_modern_client_advertised():
    """`elicit` must not refuse a client whose `_meta` advertised elicitation."""
    session = MCPSession()
    session.record_request_meta(MODERN_META)
    asked: list[str] = []

    async def requester(method: str, params: dict) -> dict:
        asked.append(method)
        return {"action": "accept", "content": {"name": "ada"}}

    ctx = MCPContext("ask", session=session, requester=requester)
    result = await ctx.elicit("Your name?", requested_schema={"type": "object"})
    assert asked == ["elicitation/create"]
    assert result["action"] == "accept"


async def test_a_gate_still_refuses_a_capability_the_client_did_not_advertise():
    """The same client advertised no sampling, so `sample` must still refuse."""
    session = MCPSession()
    session.record_request_meta(MODERN_META)

    async def requester(method: str, params: dict) -> dict:  # pragma: no cover
        raise AssertionError("the gate should have refused before requesting")

    ctx = MCPContext("draft", session=session, requester=requester)
    with pytest.raises(MCPCapabilityError):
        await ctx.sample([{"role": "user", "content": "hi"}], max_tokens=10)


async def test_the_gate_refuses_when_no_identity_was_ever_recorded():
    """A bare session advertises nothing, so every gated request refuses."""

    async def requester(method: str, params: dict) -> dict:  # pragma: no cover
        raise AssertionError("unreachable")

    ctx = MCPContext("ask", session=MCPSession(), requester=requester)
    with pytest.raises(MCPCapabilityError):
        await ctx.elicit("Your name?", requested_schema={"type": "object"})
