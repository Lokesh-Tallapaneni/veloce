"""Dual-era MCP: the 2026-07-28 revision alongside the handshake revisions.

The modern revision drops the `initialize` handshake and the protocol-level
session: a client declares its version, identity and capabilities in `_meta` on
every request, and the server answers each one independently. Both eras are
served from the same endpoint, selected by how the client opens.
"""

from __future__ import annotations

import orjson
import pytest

from veloce import Veloce
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
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy", "version": "1"},
                },
            },
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
    assert error["code"] == -32022
    assert error["data"]["requested"] == version
    assert error["data"]["supported"] == list(SERVED_PROTOCOL_VERSIONS)


async def test_the_legacy_handshake_still_serves_its_own_revision():
    out = await _drive(
        _app(),
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy", "version": "1"},
                },
            }
        ],
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
