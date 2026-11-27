"""Dual-era MCP: the 2026-07-28 revision alongside the handshake revisions.

The modern revision drops the `initialize` handshake and the protocol-level
session: a client declares its version, identity and capabilities in `_meta` on
every request, and the server answers each one independently. Both eras are
served from the same endpoint, selected by how the client opens.
"""

from __future__ import annotations

import orjson
import pytest

from tests._mcp import INVALID_PARAMS, UNSUPPORTED_PROTOCOL_VERSION, initialize
from veloce import TestClient, Veloce
from veloce.contrib.mcp.errors import ProtocolVersionError
from veloce.contrib.mcp.server import (
    MODERN_PROTOCOL_VERSION,
    SERVED_PROTOCOL_VERSIONS,
    MCPServer,
)
from veloce.contrib.mcp.transports.stdio import StdioTransport

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "ExampleClient", "version": "1.0.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _app() -> Veloce:
    app = Veloce(title="WeatherServer", description="Weather utilities.", openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    return app


async def _drive(app: Veloce, messages: list[dict]) -> list[dict]:
    inbox = [orjson.dumps(m) for m in messages]
    outbox: list[dict] = []

    async def read_line():
        return inbox.pop(0) if inbox else None

    async def write_line(data: bytes):
        outbox.append(orjson.loads(data))

    await StdioTransport(MCPServer(app), read_line, write_line).serve()
    return outbox


async def test_discover_advertises_versions_capabilities_and_identity():
    out = await _drive(
        _app(),
        [
            {
                "jsonrpc": "2.0",
                "id": "d1",
                "method": "server/discover",
                "params": {"_meta": MODERN_META},
            }
        ],
    )
    result = out[0]["result"]
    assert result["supportedVersions"] == list(SERVED_PROTOCOL_VERSIONS)
    assert result["supportedVersions"][0] == MODERN_PROTOCOL_VERSION
    assert "tools" in result["capabilities"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "WeatherServer"
    assert result["instructions"] == "Weather utilities."


async def test_a_modern_call_needs_no_handshake():
    """No `initialize`, no session - the request stands on its own."""
    out = await _drive(
        _app(),
        [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "add", "arguments": {"a": 2, "b": 3}, "_meta": MODERN_META},
            }
        ],
    )
    assert "error" not in out[0]
    assert '"sum":5' in out[0]["result"]["content"][0]["text"]


async def test_every_modern_result_carries_result_type():
    out = await _drive(
        _app(),
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN_META}}],
    )
    assert out[0]["result"]["resultType"] == "complete"


async def test_a_legacy_result_never_carries_result_type():
    """`resultType` is a modern-only field; leaking it into a handshake-era
    result would hand a legacy client something its revision does not define."""
    out = await _drive(
        _app(),
        [
            initialize("2025-11-25", id=1, client_info={"name": "legacy", "version": "1"}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ],
    )
    assert "resultType" not in out[0]["result"]
    assert "resultType" not in out[-1]["result"]


@pytest.mark.parametrize("version", ["1900-01-01", "2025-03-26", ""])
async def test_an_unserved_version_is_rejected_recoverably(version: str):
    """The error names what the server does serve, so the client can retry
    instead of failing outright."""
    out = await _drive(
        _app(),
        [
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/list",
                "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": version}},
            }
        ],
    )
    error = out[0]["error"]
    assert error["code"] == UNSUPPORTED_PROTOCOL_VERSION
    assert error["data"]["requested"] == version
    assert error["data"]["supported"] == list(SERVED_PROTOCOL_VERSIONS)


async def test_the_legacy_handshake_still_serves_its_own_revision():
    out = await _drive(
        _app(),
        [initialize(id=1, client_info={"name": "legacy", "version": "1"})],
    )
    assert out[0]["result"]["protocolVersion"] == "2025-06-18"


async def test_a_modern_notification_with_a_bad_version_gets_no_response():
    """A notification carries no id, so it can never be answered - including
    with an error."""
    out = await _drive(
        _app(),
        [
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {
                    "requestId": 1,
                    "_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"},
                },
            }
        ],
    )
    assert out == []


@pytest.mark.parametrize("version", SERVED_PROTOCOL_VERSIONS)
def test_the_http_transport_accepts_a_served_version_header(version):
    """On HTTP the version also travels in `MCP-Protocol-Version`.

    Rejecting a served value there would block the client before dispatch. The
    check this used to make - that each served version is in the frozenset the
    source builds *from those versions* - could not fail; this drives the
    transport.
    """
    app = _app()
    app.mount_mcp(transport="http", path="/mcp")

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "MCP-Protocol-Version": version,
                # The modern revision cross-checks the method against the body
                # as an anti-smuggling measure, so it travels too.
                "MCP-Method": "ping",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code == 200, response.body
    # The reply may be a bare JSON body or an SSE frame depending on what the
    # client accepts; either way it must not carry a JSON-RPC error.
    assert b'"error"' not in response.body, response.body


def test_the_http_transport_refuses_an_unserved_version_header():
    """The other side of the gate, which nothing was covering."""
    app = _app()
    app.mount_mcp(transport="http", path="/mcp")

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "MCP-Protocol-Version": "1999-01-01",
                "MCP-Method": "ping",
                "Accept": "application/json, text/event-stream",
            },
        )

    # Not `status >= 400 or "error" in body`: an unrelated 500 or a generic
    # -32603 satisfies that while the gate misattributes its refusal.
    assert response.status_code == 400, response.body
    error = response.json()["error"]
    assert error["code"] == ProtocolVersionError.code, error
    assert "1999-01-01" in error["message"], error


# ── Argument validation is a tool execution error ─────────────────


async def _call_tool(app: Veloce, name: str, arguments: dict) -> dict:
    out = await _drive(
        app,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments, "_meta": MODERN_META},
            }
        ],
    )
    return out[-1]


async def test_a_bad_argument_is_reported_in_band_not_on_the_error_channel():
    """The spec splits tool failures in two, and clients feed only *execution*
    errors back to the model. A bad argument on the JSON-RPC error channel is
    the one thing the model could have fixed, reported where it will not see
    it."""
    out = await _call_tool(_app(), "add", {"a": "not-an-int", "b": 3})
    assert "error" not in out
    assert out["result"]["isError"] is True


async def test_the_message_names_the_offending_argument():
    out = await _call_tool(_app(), "add", {"a": "not-an-int", "b": 3})
    text = out["result"]["content"][0]["text"]
    assert "a" in text
    assert "'loc'" not in text, "a Python repr is not something a model can act on"


async def test_a_missing_argument_names_what_is_missing():
    out = await _call_tool(_app(), "add", {"a": 1})
    assert out["result"]["isError"] is True
    assert "b" in out["result"]["content"][0]["text"]


async def test_an_unknown_tool_stays_on_the_protocol_error_channel():
    """Unknown tool is a protocol error per the spec - the model cannot fix it
    by adjusting arguments, so it does not belong in-band."""
    out = await _call_tool(_app(), "nosuch", {})
    assert out["error"]["code"] == INVALID_PARAMS


async def test_the_validation_message_is_not_redacted_when_debug_is_off():
    """A handler exception is redacted because it may carry a secret. A
    validation message is built from the tool's own schema and the caller's own
    input, so redacting it would leave the model nothing to correct."""
    app = _app()
    assert app.debug is False
    out = await _call_tool(app, "add", {"a": "not-an-int", "b": 3})
    text = out["result"]["content"][0]["text"]
    assert "internal error" not in text
