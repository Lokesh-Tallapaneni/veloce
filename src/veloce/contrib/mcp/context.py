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

Over a bidirectional transport the context also issues server->client requests:
`sample` (``sampling/createMessage``), `elicit` (``elicitation/create``), and
`roots` (``roots/list``). Each is gated on the client having advertised the
matching capability in ``initialize``; a handler calling one against a client that
did not advertise it raises `MCPCapabilityError`, and a handler calling one off a
bidirectional transport raises `RuntimeError`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from veloce.contrib.mcp.errors import MCPCapabilityError

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
        "_requester",
        "_client_capabilities",
    )

    def __init__(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        notifier: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        progress_token: str | int | None = None,
        log_level: str | None = None,
        requester: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        client_capabilities: dict[str, Any] | None = None,
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
        # Server->client request issuer (wired only by a bidirectional transport;
        # `None` for a one-way or off-transport construction) and the capabilities
        # the client advertised in `initialize` - together they make `sample` /
        # `elicit` / `roots` live, gated on the matching client capability.
        self._requester = requester
        self._client_capabilities: dict[str, Any] = client_capabilities or {}

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

    # ── Server-initiated requests ─────────────────────────────

    async def sample(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        model_preferences: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ask the client's LLM to sample a completion (sampling/createMessage).

        Returns the client's result (its chosen model, role, and content). Requires
        a bidirectional transport and a client that advertised the ``sampling``
        capability; `tools` / `tool_choice` additionally require ``sampling.tools``.
        """
        params: dict[str, Any] = {"messages": messages, "maxTokens": max_tokens}
        if model_preferences is not None:
            params["modelPreferences"] = model_preferences
        if system_prompt is not None:
            params["systemPrompt"] = system_prompt
        if temperature is not None:
            params["temperature"] = temperature
        if stop_sequences is not None:
            params["stopSequences"] = stop_sequences
        if tools is not None or tool_choice is not None:
            self._require_sub_capability("sampling", "tools")
            if tools is not None:
                params["tools"] = tools
            if tool_choice is not None:
                params["toolChoice"] = tool_choice
        if metadata is not None:
            params["metadata"] = metadata
        return await self._request("sampling/createMessage", "sampling", params)

    async def elicit(
        self,
        message: str,
        *,
        requested_schema: dict[str, Any] | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        """Ask the client to gather input from its user (elicitation/create).

        Form mode passes a `requested_schema` (the JSON Schema of the fields to
        collect); URL mode passes a `url` the client opens instead. Returns the
        client's response (its action and any collected content). Requires a
        bidirectional transport and a client that advertised ``elicitation``.
        """
        if requested_schema is not None and url is not None:
            raise ValueError("elicit takes either requested_schema (form) or url, not both")
        params: dict[str, Any] = {"message": message}
        if url is not None:
            params["mode"] = "url"
            params["url"] = url
        elif requested_schema is not None:
            params["requestedSchema"] = requested_schema
        return await self._request("elicitation/create", "elicitation", params)

    async def roots(self) -> list[dict[str, Any]]:
        """List the client's exposed filesystem roots (roots/list).

        Returns the client's `roots` array. Requires a bidirectional transport and
        a client that advertised the ``roots`` capability.
        """
        result = await self._request("roots/list", "roots", {})
        roots = result.get("roots")
        return roots if isinstance(roots, list) else []

    async def _request(
        self, method: str, capability: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Issue a server->client request, gated on the client's advertised capability."""
        if self._requester is None:
            raise RuntimeError(f"{method} requires a bidirectional transport")
        if capability not in self._client_capabilities:
            raise MCPCapabilityError(capability)
        return await self._requester(method, params)

    def _require_sub_capability(self, capability: str, sub: str) -> None:
        """Reject a call needing a sub-capability the client did not advertise."""
        advertised = self._client_capabilities.get(capability)
        if not isinstance(advertised, dict) or sub not in advertised:
            raise MCPCapabilityError(f"{capability}.{sub}")

    def __repr__(self) -> str:
        return f"MCPContext(tool_name={self.tool_name!r})"
