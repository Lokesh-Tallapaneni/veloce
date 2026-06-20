"""MCPContext — request-scoped handle passed to an MCP tool invocation.

One `MCPContext` is constructed per `tools/call`, mirroring how a `Request` is
constructed per HTTP request. A tool handler (or one of its `Depends`) may declare
a parameter typed `MCPContext` to receive it (detected by that type annotation,
never by parameter name, so a plain argument named ``ctx`` / ``context`` stays a
normal tool input). The context carries the calling tool name and the raw argument
mapping, and - when the server is served over a transport with an outbound
notification channel - its `log` and `report_progress` methods send live
``notifications/message`` and ``notifications/progress`` to the client. Off a
transport (a bare construction) they are inert. When the client sends a
``notifications/cancelled`` naming this call's request id, the server marks the
context cancelled (so a cooperative handler can poll `cancelled` and stop) and
cancels the in-flight task.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# RFC 5424 severity order, the scale MCP uses for ``logging/setLevel`` and
# ``notifications/message``. A log message below the client's set minimum level is
# dropped.
_LOG_RANKS = {
    "debug": 0,
    "info": 1,
    "notice": 2,
    "warning": 3,
    "error": 4,
    "critical": 5,
    "alert": 6,
    "emergency": 7,
}


class MCPContext:
    """Per-invocation context for an MCP tool call.

    Usage::

        @app.mcp_tool(description="Look up a user by id")
        async def get_user(user_id: int, ctx: MCPContext) -> dict:
            await ctx.log("info", f"looking up {user_id}")
            await ctx.report_progress(1, 2)
            return {"id": user_id}
    """

    __slots__ = (
        "tool_name",
        "arguments",
        "_cancelled",
        "_notifier",
        "_progress_token",
        "_log_level",
    )

    def __init__(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        notifier: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        progress_token: str | int | None = None,
        log_level: str | None = None,
    ) -> None:
        self.tool_name = tool_name
        # The raw, un-coerced argument mapping the client sent in ``tools/call``.
        # The resolver coerces individual values onto the handler signature; this
        # stays available for handlers that want the untouched payload.
        self.arguments: dict[str, Any] = arguments or {}
        self._cancelled = False
        # Outbound notification sink (wired by the transport; `None` for a bare
        # off-transport construction), the call's progress token, and the current
        # minimum log level - together they make `log` / `report_progress` live.
        self._notifier = notifier
        self._progress_token = progress_token
        self._log_level = log_level

    @property
    def cancelled(self) -> bool:
        """Whether the client has sent ``notifications/cancelled`` for this call."""
        return self._cancelled

    def _mark_cancelled(self) -> None:
        """Record that the client cancelled this call (set by the server)."""
        self._cancelled = True

    async def log(self, level: str, message: Any, logger: str | None = None) -> None:
        """Send a log message to the MCP client (notifications/message).

        Dropped when no notification channel is wired, or when `level` is below the
        client's `logging/setLevel` minimum.
        """
        if self._notifier is None:
            return
        if self._log_level is not None and _LOG_RANKS.get(level, 0) < _LOG_RANKS.get(
            self._log_level, 0
        ):
            return
        params: dict[str, Any] = {"level": level, "data": message}
        if logger is not None:
            params["logger"] = logger
        await self._notifier(
            {"jsonrpc": "2.0", "method": "notifications/message", "params": params}
        )

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        """Report progress to the MCP client (notifications/progress).

        Dropped when no notification channel is wired, or when the client did not
        send a `progressToken` with the call (progress is only reported on request).
        """
        if self._notifier is None or self._progress_token is None:
            return
        params: dict[str, Any] = {"progressToken": self._progress_token, "progress": progress}
        if total is not None:
            params["total"] = total
        if message is not None:
            params["message"] = message
        await self._notifier(
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": params}
        )

    def __repr__(self) -> str:
        return f"MCPContext(tool_name={self.tool_name!r})"
