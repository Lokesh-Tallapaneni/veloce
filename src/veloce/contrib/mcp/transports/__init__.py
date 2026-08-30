"""MCP transports — the wire layer between a client and the `MCPServer`.

Ships the stdio transport (`StdioTransport`, `serve_stdio`), the Streamable HTTP
transport (`register_http_transport`), and the deprecated split-endpoint SSE
transport (`register_sse_transport`) for clients that speak only the older wire.
All three satisfy the `Transport` contract in `base.py`, so the server pushes
outbound notifications without knowing the wire.

`SessionBackend` and `SessionRecord` are the seam for sharing HTTP sessions
between workers; implement the first over your own store to hand the second
around.
"""

from __future__ import annotations

from veloce.contrib.mcp.transports.base import BidirectionalTransport, Transport
from veloce.contrib.mcp.transports.http import register_http_transport
from veloce.contrib.mcp.transports.session_store import SessionBackend, SessionRecord
from veloce.contrib.mcp.transports.sse import register_sse_transport
from veloce.contrib.mcp.transports.stdio import MCPRequestError, StdioTransport, serve_stdio

__all__ = [
    "BidirectionalTransport",
    "MCPRequestError",
    "SessionBackend",
    "SessionRecord",
    "StdioTransport",
    "Transport",
    "register_http_transport",
    "register_sse_transport",
    "serve_stdio",
]
