"""MCP session tests - client-capability recording and lifecycle ordering."""

from __future__ import annotations

import asyncio

import orjson

from veloce import Veloce
from veloce.contrib.mcp import MCPSession
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.transports.stdio import StdioTransport


class _Pipe:
    """Drive a `StdioTransport` in-process: feed request lines, collect replies."""

    def __init__(self, server: MCPServer) -> None:
        self._inbox: list[bytes] = []
        self.outbox: list[dict] = []
        self.transport = StdioTransport(server, self._read_line, self._write_line)

    def feed(self, message: dict) -> None:
        self._inbox.append(orjson.dumps(message))

    async def _read_line(self) -> bytes | None:
        if not self._inbox:
            return None
        return self._inbox.pop(0)

    async def _write_line(self, data: bytes) -> None:
        self.outbox.append(orjson.loads(data))

    async def run(self) -> list[dict]:
        await self.transport.serve()
        return self.outbox


def _strict_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["MCP_ENFORCE_LIFECYCLE"] = True
    return app


# -- Client capability recording --------------------------------------


def test_session_records_client_capabilities():
    session = MCPSession()
    server = MCPServer(Veloce(openapi_url=None))
    params = {"capabilities": {"sampling": {}, "roots": {"listChanged": True}}}
    asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}, session
        )
    )
    assert session.client_capabilities == {"sampling": {}, "roots": {"listChanged": True}}
    assert session.supports("sampling") is True
    assert session.supports("elicitation") is False


def test_session_records_client_info():
    session = MCPSession()
    server = MCPServer(Veloce(openapi_url=None))
    params = {"clientInfo": {"name": "agent", "version": "9.9"}, "capabilities": {}}
    asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}, session
        )
    )
    assert session.client_info == {"name": "agent", "version": "9.9"}


def test_session_defaults_when_no_capabilities_sent():
    session = MCPSession()
    server = MCPServer(Veloce(openapi_url=None))
    asyncio.run(
        server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, session
        )
    )
    assert session.client_capabilities == {}
    assert session.client_info is None
    assert session.supports("sampling") is False


def test_initialized_notification_marks_session():
    session = MCPSession()
    server = MCPServer(Veloce(openapi_url=None))
    asyncio.run(
        server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    )
    assert session.initialized is True


def test_stdio_loop_records_capabilities_across_messages():
    app = Veloce(openapi_url=None)
    pipe = _Pipe(MCPServer(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"sampling": {}}},
        }
    )
    pipe.feed({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    out = asyncio.run(pipe.run())
    # The single connection serves both messages from one session; recording the
    # capabilities does not disturb the second call's normal result.
    assert out[0]["id"] == 1
    assert "tools" in out[1]["result"]


# -- Lifecycle ordering (opt-in) --------------------------------------


def test_pre_initialize_request_rejected_when_enforced():
    pipe = _Pipe(MCPServer(_strict_app()))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    out = asyncio.run(pipe.run())
    assert out[0]["error"]["code"] == -32600
    assert "before initialization" in out[0]["error"]["message"]


def test_ping_allowed_before_initialize_when_enforced():
    pipe = _Pipe(MCPServer(_strict_app()))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    out = asyncio.run(pipe.run())
    assert out[0]["result"] == {}


def test_initialize_allowed_first_when_enforced():
    pipe = _Pipe(MCPServer(_strict_app()))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    out = asyncio.run(pipe.run())
    assert "protocolVersion" in out[0]["result"]


def test_requests_allowed_after_initialize_when_enforced():
    pipe = _Pipe(MCPServer(_strict_app()))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    pipe.feed({"jsonrpc": "2.0", "method": "notifications/initialized"})
    pipe.feed({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    out = asyncio.run(pipe.run())
    assert "tools" in out[1]["result"]


def test_notification_passes_before_initialize_when_enforced():
    # A one-way message is not ordered by the initialization rule, so a stray
    # notification before initialize is swallowed (no response), not rejected.
    pipe = _Pipe(MCPServer(_strict_app()))
    pipe.feed({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
    out = asyncio.run(pipe.run())
    assert out == []


def test_lifecycle_not_enforced_by_default():
    # Without the opt-in flag the stdio loop answers a pre-initialize request
    # exactly as before, so existing clients are unaffected.
    app = Veloce(openapi_url=None)
    pipe = _Pipe(MCPServer(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    out = asyncio.run(pipe.run())
    assert "tools" in out[0]["result"]


# -- Stateless HTTP fast path is unaffected ---------------------------


def test_http_path_passes_no_session():
    # `handle_message` with no session never gates: a pre-initialize call on the
    # stateless transport returns its normal result.
    server = MCPServer(_strict_app())
    out = asyncio.run(
        server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    )
    assert "tools" in out["result"]
