"""Model Context Protocol integration - every Veloce route is also an AI tool.

`veloce.contrib.mcp` exposes a Veloce app's handlers as Model Context Protocol
tools so an AI agent can call them over JSON-RPC 2.0. Register MCP-only tools
with `@app.mcp_tool(...)`, opt an existing route in with
`expose_as_mcp_tool=True` / `mcp_description=...`, then serve over stdio with
`app.mount_mcp(transport="stdio")`.

v1 scope: tools only, stdio transport only. Resources, prompts, and the
HTTP/SSE transport are v2.
"""

from __future__ import annotations

from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.registry import MCPTool, ToolRegistry, build_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.transports.stdio import StdioTransport, serve_stdio

__all__ = [
    "MCPContext",
    "MCPServer",
    "MCPTool",
    "StdioTransport",
    "ToolRegistry",
    "build_registry",
    "serve_stdio",
]
