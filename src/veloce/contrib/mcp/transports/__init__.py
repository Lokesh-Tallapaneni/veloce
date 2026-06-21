"""MCP transports — the wire layer between a client and the `MCPServer`.

Ships the stdio transport (`StdioTransport`, `serve_stdio`) and the Streamable
HTTP transport (`register_http_transport`). Both satisfy the `Transport` contract
in `base.py`, so the server pushes outbound notifications without knowing the wire.
"""

from __future__ import annotations

from veloce.contrib.mcp.transports.base import BidirectionalTransport, Transport
from veloce.contrib.mcp.transports.http import register_http_transport
from veloce.contrib.mcp.transports.stdio import StdioTransport, serve_stdio

__all__ = [
    "BidirectionalTransport",
    "StdioTransport",
    "Transport",
    "register_http_transport",
    "serve_stdio",
]
