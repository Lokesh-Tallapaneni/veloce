"""MCP transports - the wire layer between a client and the `MCPServer`.

v1 ships the stdio transport only; HTTP / SSE transports are v2.
"""

from __future__ import annotations

from veloce.contrib.mcp.transports.stdio import StdioTransport, serve_stdio

__all__ = ["StdioTransport", "serve_stdio"]
