"""Model Context Protocol integration - every Veloce route is also an AI tool.

`veloce.contrib.mcp` exposes a Veloce app's handlers as Model Context Protocol
tools so an AI agent can call them over JSON-RPC 2.0. Register MCP-only tools
with `@app.mcp_tool(...)`, opt an existing route in with
`expose_as_mcp_tool=True` / `mcp_description=...`, then serve over stdio with
`app.mount_mcp(transport="stdio")`.

Scope: tools and resources over the stdio transport. The server negotiates the
protocol version with the client, answers ``ping``, and a tool definition carries
HTTP-derived annotation hints (read-only / idempotent / destructive), a `title`,
and - where the result has a declared object shape - an `outputSchema` whose
structured value `tools/call` returns alongside the text block. A read-only route
flagged ``expose_as_mcp_resource=True`` is served as a resource (``resources/list``,
``resources/templates/list``, ``resources/read``); a tool returning an image or
audio response emits the matching typed content block. Prompts and the Streamable
HTTP transport are not yet implemented.
"""

from __future__ import annotations

from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.registry import MCPTool, ToolRegistry, build_registry
from veloce.contrib.mcp.resources import (
    MCPResource,
    ResourceRegistry,
    build_resource_registry,
)
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.transports.stdio import StdioTransport, serve_stdio

__all__ = [
    "MCPContext",
    "MCPResource",
    "MCPServer",
    "MCPTool",
    "ResourceRegistry",
    "StdioTransport",
    "ToolRegistry",
    "build_registry",
    "build_resource_registry",
    "serve_stdio",
]
