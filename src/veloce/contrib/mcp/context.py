"""MCPContext — request-scoped handle passed to an MCP tool invocation.

One `MCPContext` is constructed per `tools/call`. A tool handler (or one of its
`Depends`) receives it by declaring a parameter typed `MCPContext` - detected by
that annotation, never by parameter name, so an argument named ``ctx`` stays a
normal tool input. Over a transport with an outbound notification channel `log`
and `report_progress` send live ``notifications/message`` and
``notifications/progress``; off a transport they are inert.

Over a bidirectional transport the context also issues server->client requests:
`sample` (``sampling/createMessage``), `elicit` (``elicitation/create``), and
`roots` (``roots/list``). Each is gated on the client having advertised the
matching capability in ``initialize``, raising `MCPCapabilityError` when it did
not and `RuntimeError` off a bidirectional transport.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from veloce._internal import _current_request_var
from veloce.contrib.mcp._context_requests import _error_content as _error_content
from veloce.contrib.mcp._context_requests import _ServerRequestsMixin
from veloce.contrib.mcp.errors import MCPCapabilityError
from veloce.principal import current_principal

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MCPServer
    from veloce.contrib.mcp.session import MCPSession

# One per kind of primitive whose listing changed: sending all three would use
# capabilities `initialize` never negotiated, which the lifecycle rules forbid.
_LIST_CHANGED_BY_KIND = (
    ("tools", "notifications/tools/list_changed"),
    ("prompts", "notifications/prompts/list_changed"),
    ("resources", "notifications/resources/list_changed"),
)

# ── Per-call state a handler reads through `MCPContext` ─────

# The dispatch core's own per-call vars live in `_helpers` instead.

# Whether the current call is running as a background task rather than inline.
_in_task_var: ContextVar[bool] = ContextVar("_mcp_in_task", default=False)

# The task this call runs as, and the `tools/call` that created it.
_task_id_var: ContextVar[str | None] = ContextVar("_mcp_task_id", default=None)
_origin_request_id_var: ContextVar[Any] = ContextVar("_mcp_origin_request_id", default=None)

# Set once by each transport; `None` for a bare off-transport construction.
_transport_var: ContextVar[str | None] = ContextVar("_mcp_transport", default=None)

# `_meta` to send back on this call's result; `None` when none was asked for.
_result_meta_var: ContextVar[dict[str, Any] | None] = ContextVar("_mcp_result_meta", default=None)

# The connection being served, or `None` off a stateful transport.
_session_var: ContextVar[MCPSession | None] = ContextVar("_mcp_session", default=None)


# ── Logging levels ──────────────────────────────────────────

LOG_LEVEL_OFF: str = "off"
"""Suppresses every `notifications/message`: the spec requires a server to send none for a request that named no level, which no RFC 5424 level can express."""

# RFC 5424 severity order, the scale MCP uses for ``logging/setLevel``.
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


class MCPContext(_ServerRequestsMixin):
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
        "_session",
        "_server",
        "_request_meta",
        "_request_id",
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
        session: MCPSession | None = None,
        server: MCPServer | None = None,
        request_meta: dict[str, Any] | None = None,
        request_id: Any = None,
    ) -> None:
        self.tool_name = tool_name
        # The raw, un-coerced mapping the client sent in ``tools/call``.
        self.arguments: dict[str, Any] = arguments or {}
        self._cancelled = False
        # `None` off a transport, which makes `log` / `report_progress` inert.
        self._notifier = notifier
        self._progress_token = progress_token
        # The `_meta` the client sent with this request, verbatim.
        self._request_meta = request_meta
        # Not read from the `_helpers` var, since that module imports this one.
        self._request_id = request_id
        self._log_level = log_level
        # Wired only by a bidirectional transport, which is what makes `sample` /
        # `elicit` / `roots` available, gated on the matching client capability.
        self._requester = requester
        # References, so a call that never asks pays only the two assignments.
        self._session = session
        self._server = server
        # Resolved once here rather than on every capability gate.
        if client_capabilities is not None:
            self._client_capabilities: dict[str, Any] = client_capabilities
        elif session is not None:
            self._client_capabilities = session.client_capabilities
        else:
            self._client_capabilities = {}

    @property
    def cancelled(self) -> bool:
        """Whether the client has sent ``notifications/cancelled`` for this call."""
        return self._cancelled

    def _mark_cancelled(self) -> None:
        """Record that the client cancelled this call (set by the server)."""
        self._cancelled = True

    @property
    def state(self) -> Any:
        """Scratch space shared by everything resolving this one call.

        The same object a handler declaring `request: Request` reaches through
        `request.state`. Scoped to the call: a later `tools/call` starts clean.

        Usage::

            @app.mcp_tool(description="Look something up")
            async def lookup(ctx: MCPContext) -> dict:
                ctx.state.started = time.monotonic()
                return {"ok": True}
        """
        request = _current_request_var.get()
        if request is None:
            raise RuntimeError(
                "MCPContext.state needs the request being handled, which a bare "
                "MCPContext has no reference to. It is wired for a real tool "
                "invocation."
            )
        return request.state

    # ── Call metadata ─────────────────────────────────────────

    @property
    def session_id(self) -> str | None:
        """The dispatching connection's id, or None on the stateless path.

        Unique across processes, so it is a safe key for per-client state under a
        multi-worker server. It identifies a *connection*, not a client, so a
        reconnecting client gets a new one.
        """
        session = self._session
        return session.public_id if session is not None else None

    @property
    def client_info(self) -> dict[str, Any]:
        """The client's `implementation` block from `initialize`, or empty."""
        session = self._session
        return (session.client_info or {}) if session is not None else {}

    @property
    def client_capabilities(self) -> dict[str, Any]:
        """The capabilities the client advertised, or empty off a stateful transport."""
        return self._client_capabilities

    @property
    def is_background_task(self) -> bool:
        """Whether this call is running as a task rather than inline."""
        return _in_task_var.get()

    @property
    def request_meta(self) -> dict[str, Any]:
        """The `_meta` the client sent with this request, or an empty mapping.

        The protocol reserves `_meta` for metadata it does not define - a
        progress token, a trace id, an extension's own block.
        """
        return self._request_meta or {}

    @property
    def client_id(self) -> str | None:
        """The authenticated caller's id, or None when the call is unauthenticated.

        The subject of the principal the transport established. `client_info` is
        what the client *said* it was at `initialize`; this is what it proved.
        """
        principal = current_principal()
        return getattr(principal, "subject", None) if principal is not None else None

    @property
    def request_id(self) -> Any:
        """The JSON-RPC id of the call being served, or None for a notification."""
        return self._request_id

    @property
    def origin_request_id(self) -> Any:
        """The id of the `tools/call` that created this task, or None inline.

        A background task outlives the request that started it, so its own
        `request_id` is not the one the client is correlating against.
        """
        return _origin_request_id_var.get()

    @property
    def task_id(self) -> str | None:
        """The id of the task this call is running as, or None when inline.

        The same handle the client polls with `tasks/get`, so a handler can
        record it against whatever it writes.
        """
        return _task_id_var.get()

    @property
    def transport(self) -> str | None:
        """The transport serving this call - `"stdio"`, `"http"` or `"sse"`.

        `None` off a transport. For whether a server-initiated request can reach
        the client, ask `client_supports(...)`; this is for diagnostics.
        """
        return _transport_var.get()

    @property
    def lifespan_context(self) -> Any:
        """The application state established at startup.

        The same `app.state` an HTTP handler reaches, so a pool opened in a
        lifespan hook is reached the same way through either door. `None` off a
        server.
        """
        server = self._server
        return getattr(server.app, "state", None) if server is not None else None

    def client_supports(self, capability: str) -> bool:
        """Return whether the client advertised `capability` (dotted for nested).

        `ctx.client_supports("sampling")` and `ctx.client_supports("sampling.tools")`
        both work; the same lookup the server-initiated requests gate on.
        """
        node: Any = self._client_capabilities
        for part in capability.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return node is not False

    # ── Logging ───────────────────────────────────────────────

    async def debug(self, message: Any, logger: str | None = None) -> None:
        """Send a debug-level log message to the client.

        Args:
            message: The payload sent as the record's `data`; any JSON-serialisable value.
            logger: Optional logger name the client can filter on.

        """
        await self.log("debug", message, logger)

    async def info(self, message: Any, logger: str | None = None) -> None:
        """Send an info-level log message to the client.

        Args:
            message: The payload sent as the record's `data`; any JSON-serialisable value.
            logger: Optional logger name the client can filter on.

        """
        await self.log("info", message, logger)

    async def warning(self, message: Any, logger: str | None = None) -> None:
        """Send a warning-level log message to the client.

        Args:
            message: The payload sent as the record's `data`; any JSON-serialisable value.
            logger: Optional logger name the client can filter on.

        """
        await self.log("warning", message, logger)

    async def error(self, message: Any, logger: str | None = None) -> None:
        """Send an error-level log message to the client.

        Args:
            message: The payload sent as the record's `data`; any JSON-serialisable value.
            logger: Optional logger name the client can filter on.

        """
        await self.log("error", message, logger)

    async def log(self, level: str, message: Any, logger: str | None = None) -> None:
        """Send a log message to the MCP client (notifications/message).

        Dropped when no notification channel is wired, or when `level` is below the
        client's `logging/setLevel` minimum.

        Args:
            level: An MCP log level such as `debug`, `info`, `warning` or `error`.
            message: The payload sent as the record's `data`; any JSON-serialisable value.
            logger: Optional logger name the client can filter on.

        """
        if self._notifier is None or self._log_level == LOG_LEVEL_OFF:
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

        Args:
            progress: Work completed so far; it should increase with each report.
            total: Total work expected, when known.
            message: Optional human-readable status for the client to show.

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

    # ── Reading the server's own components ───────────────────

    def _require_server(self, what: str) -> MCPServer:
        """Return the serving `MCPServer`, or explain why it is unavailable."""
        server = self._server
        if server is None:
            raise RuntimeError(
                f"{what} needs the serving MCPServer, which a bare MCPContext has "
                "no reference to. It is wired for a real tool invocation."
            )
        return server

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read one of this server's registered resources by URI.

        Goes through the same handler `resources/read` serves, so the resource's
        declared scopes are enforced against the calling principal: a tool cannot
        reach a resource its caller could not have read itself.
        """
        server = self._require_server("read_resource")
        contents: dict[str, Any] = await server._resources_read({"uri": uri})
        return contents

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Render one of this server's registered prompts by name.

        Goes through the same handler `prompts/get` serves, including its scope
        check, for the same reason `read_resource` does.

        Args:
            name: Registered prompt name.
            arguments: Prompt arguments, passed through to the prompt handler.

        Returns:
            The `prompts/get` result: the rendered messages and the prompt's description.

        Raises:
            RuntimeError: If this context has no serving `MCPServer`, as with a bare
                `MCPContext`.

        """
        server = self._require_server("get_prompt")
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        rendered: dict[str, Any] = await server._prompts_get(params)
        return rendered

    def list_resources(self) -> list[dict[str, Any]]:
        """List this server's registered resources, as `resources/list` reports them.

        Routed through the one listing builder, so a primitive this connection
        hid with `hide()` is absent here too. Unpaged: a handler cannot ask again
        for the next page.
        """
        server = self._require_server("list_resources")
        resources: list[dict[str, Any]] = server._resource_listing({}, page_size=None)["resources"]
        return resources

    def list_prompts(self) -> list[dict[str, Any]]:
        """List this server's registered prompts, as `prompts/list` reports them.

        Hidden-aware and unpaged, for the reasons `list_resources` gives.
        """
        server = self._require_server("list_prompts")
        prompts: list[dict[str, Any]] = server._prompt_listing({}, page_size=None)["prompts"]
        return prompts

    async def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification to the client.

        Inert when no notification channel is wired, matching `log` and
        `report_progress`.

        Args:
            method: JSON-RPC method name of the notification.
            params: Optional notification parameters.

        """
        if self._notifier is None:
            return
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._notifier(message)

    # ── Per-connection visibility ─────────────────────────────

    async def hide(self, *names: str) -> None:
        """Hide tools, prompts or resources from this connection's listings.

        Names a tool or prompt by name and a resource by URI. The change belongs
        to the connection that made the call - another client's listings are
        untouched - and this connection is told its lists changed so it fetches
        them again.

        For a tool, hiding is enforcement too: `tools/call` rejects a hidden
        name exactly as it would an unregistered one. A hidden prompt or
        resource is not yet refused at `prompts/get`/`resources/read` the same
        way - only its listing narrows - so treat those two as visibility
        only, not a permission boundary, until that gap closes too.
        """
        await self._change_visibility(lambda hidden: hidden.update(names))

    async def unhide(self, *names: str) -> None:
        """Show primitives hidden earlier on this connection."""
        await self._change_visibility(lambda hidden: hidden.difference_update(names))

    async def reset_visibility(self) -> None:
        """Show everything this connection had hidden."""
        await self._change_visibility(lambda hidden: hidden.clear())

    async def _change_visibility(self, mutate: Callable[[set[str]], Any]) -> None:
        """Apply a change to this connection's hidden set and announce it."""
        session = _session_var.get()
        if session is None:
            raise RuntimeError(
                "visibility is a property of a connection, which a call outside a "
                "stateful transport does not have"
            )
        before = set(session.hidden)
        mutate(session.hidden)
        changed = before.symmetric_difference(session.hidden)
        if not changed:
            return
        # Only this connection, and only about the lists that changed.
        kinds = self._changed_kinds(changed)
        for kind, method in _LIST_CHANGED_BY_KIND:
            if kind in kinds:
                await self.send_notification(method, {})

    def _changed_kinds(self, names: set[str]) -> frozenset[str]:
        """Return which listings the given names appear in.

        A hidden name is a tool or prompt name, or a resource URI. Off a server
        nothing can be resolved, so every listing is treated as affected rather
        than silently telling no one.
        """
        server = self._server
        if server is None:
            return frozenset(kind for kind, _method in _LIST_CHANGED_BY_KIND)
        kinds = set()
        for name in names:
            if server.registry.get(name) is not None:
                kinds.add("tools")
            if server.prompts.get(name) is not None:
                kinds.add("prompts")
            if name in server.resources.resources:
                kinds.add("resources")
        return frozenset(kinds)

    # ── Call-scoped result metadata ───────────────────────────

    @property
    def result_meta(self) -> dict[str, Any]:
        """Scratch `_meta` sent back on this call's result.

        A handler puts here what its client agreed to read - a cost, a trace id,
        an extension's own block. Mutated in place::

            ctx.result_meta["io.example/trace"] = trace_id

        It belongs to one call: the next one starts empty. A handler that never
        touches it sends no `_meta` at all.
        """
        meta = _result_meta_var.get()
        if meta is None:
            # Only outside a served call: the server binds the slot first, so an
            # offloaded sync handler writes to a dict both contexts share.
            meta = {}
            _result_meta_var.set(meta)
        return meta

    async def _request(
        self, method: str, capability: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Issue a server->client request, gated on the client's advertised capability."""
        if self._requester is None:
            raise RuntimeError(f"{method} requires a bidirectional transport")
        # Through `client_supports`, so a client advertising `"sampling": false` -
        # explicitly declining - is not read as having the capability.
        if not self.client_supports(capability):
            raise MCPCapabilityError(capability)
        return await self._requester(method, params)

    def _require_sub_capability(self, capability: str, sub: str) -> None:
        """Reject a call needing a sub-capability the client did not advertise."""
        advertised = self._client_capabilities.get(capability)
        if not isinstance(advertised, dict) or sub not in advertised:
            raise MCPCapabilityError(f"{capability}.{sub}")

    def __repr__(self) -> str:
        return f"MCPContext(tool_name={self.tool_name!r})"
