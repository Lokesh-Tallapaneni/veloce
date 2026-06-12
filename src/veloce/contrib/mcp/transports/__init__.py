"""MCP transports — the wire layer between a client and the `MCPServer`.

Ships the stdio transport; the Streamable HTTP transport is not yet implemented.
"""

from __future__ import annotations

from veloce.contrib.mcp.transports.stdio import StdioTransport, serve_stdio

__all__ = ["StdioTransport", "serve_stdio"]
