"""MCPContext - request-scoped handle passed to an MCP tool invocation.

One `MCPContext` is constructed per `tools/call`, mirroring how a `Request`
is constructed per HTTP request. A tool handler (or one of its `Depends`)
may declare a parameter typed `MCPContext` to receive it (detected by that
type annotation, never by parameter name, so a plain argument named ``ctx`` /
``context`` stays a normal tool input). The context carries the calling tool
name, the raw argument
mapping, and placeholder hooks for the cancellation / progress / logging
channels the MCP protocol defines; these are inert no-ops on the stdio transport
so handlers written against them keep working once the channels are wired.
"""

from __future__ import annotations

from typing import Any


class MCPContext:
    """Per-invocation context for an MCP tool call.

    Usage::

        @app.mcp_tool(description="Look up a user by id")
        async def get_user(user_id: int, ctx: MCPContext) -> dict:
            await ctx.log("info", f"looking up {user_id}")
            return {"id": user_id}
    """

    __slots__ = ("tool_name", "arguments", "_cancelled")

    def __init__(self, tool_name: str, arguments: dict[str, Any] | None = None) -> None:
        self.tool_name = tool_name
        # The raw, un-coerced argument mapping the client sent in
        # ``tools/call``. The resolver coerces individual values onto the
        # handler signature; this stays available for handlers that want the
        # untouched payload.
        self.arguments: dict[str, Any] = arguments or {}
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        """Whether the caller has requested cancellation (no cancel channel yet)."""
        return self._cancelled

    async def log(self, level: str, message: str) -> None:
        """Send a log line to the MCP client - inert (no log channel yet)."""

    async def report_progress(self, progress: float, total: float | None = None) -> None:
        """Report progress to the MCP client - inert (no progress channel yet)."""

    def __repr__(self) -> str:
        return f"MCPContext(tool_name={self.tool_name!r})"
