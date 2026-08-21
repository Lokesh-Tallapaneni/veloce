"""MCPServer — dispatch JSON-RPC 2.0 method calls against the tool registry.

The server is transport-agnostic: a transport (stdio) hands it decoded JSON-RPC
request objects, forwards the responses it returns, and supplies the outbound sink
(`set_notifier`) the server pushes one-way notifications through. It implements
``initialize`` (negotiating the protocol version), ``ping``, the tool methods
(``tools/list`` / ``tools/call``), the resource methods (``resources/list`` /
``resources/templates/list`` / ``resources/read``), the prompt methods
(``prompts/list`` / ``prompts/get``), ``logging/setLevel``, the
``notifications/initialized`` ack, and ``notifications/cancelled`` (cancelling the
named in-flight request). A ``tools/call`` runs the handler through the
shared `DependencyResolver`, so `Depends()` graphs, `yield`-style teardown, and
`Security` all behave exactly as on the HTTP and WebSocket paths; resource reads
and prompt renders replay the same invocation path. Per-tool instrumentation fires
through the same `app.add_instrumentation` hook the request path uses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, cast

from veloce import status
from veloce._internal import _is_async_callable, offload
from veloce.contrib.mcp._helpers import (
    DEFERRED_RESPONSE,
    _binary_result,
    _describe_prompt,
    _describe_resource,
    _describe_resource_template,
    _InFlight,
    _inflight_var,
    _log_level_var,
    _normalize_prompt_messages,
    _notifier_var,
    _principal_lacks_scopes,
    _progress_token,
    _request_id_var,
    _requester_var,
    _resource_contents,
    _response_body_value,
    _RouteResponse,
    _session_var,
    _ShortCircuit,
    _stringify,
    _text_result,
    _tool_annotations,
)
from veloce.contrib.mcp._invocation import InvocationMixin
from veloce.contrib.mcp._tasks import TasksMixin
from veloce.contrib.mcp.capabilities import (
    Capability,
    LoggingCapability,
    PromptsCapability,
    ResourcesCapability,
    ToolsCapability,
)
from veloce.contrib.mcp.completion import CompletionsCapability, attach_completers
from veloce.contrib.mcp.context import _LOG_RANKS, LOG_LEVEL_OFF
from veloce.contrib.mcp.errors import (
    _JSONRPC_INTERNAL_ERROR,
    _JSONRPC_INVALID_REQUEST,
    _JSONRPC_METHOD_NOT_FOUND,
    AuthorizationError,
    InternalError,
    InvalidParamsError,
    MCPError,
    ResourceNotFoundError,
    UnsupportedProtocolVersionError,
    _error,
    _ForbiddenError,
    _InBandError,
    _InvalidArgumentsError,
)
from veloce.contrib.mcp.icons import render_icons
from veloce.contrib.mcp.prompts import PromptRegistry, build_prompt_registry
from veloce.contrib.mcp.registry import ToolFilter, ToolRegistry, build_registry
from veloce.contrib.mcp.resources import ResourceRegistry, build_resource_registry
from veloce.contrib.mcp.subscriptions import (
    ConnectionRegistry,
    SubscriptionsCapability,
    subscription_closed_response,
)
from veloce.contrib.mcp.tasks import TaskRegistry, TasksCapability
from veloce.http.response import Response
from veloce.principal import current_principal

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.registry import MCPTool
    from veloce.contrib.mcp.session import MCPSession

_logger = logging.getLogger(__name__)

# A dispatch entry: an async handler taking the request `params` and returning the
# JSON-RPC `result` object (or `None` for a method that produces no response, such
# as the `notifications/initialized` ack). The dispatch map maps method names to
# these so `handle_message` is one dict lookup, not an if/elif ladder.
MethodHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]

# Latest Model Context Protocol revision this server speaks. Returned from
# ``initialize`` when the client requests a revision this server does not
# recognise, per the MCP lifecycle spec (the client then decides whether to
# proceed). The tools surface is stable across the supported revisions.
LATEST_PROTOCOL_VERSION = "2025-11-25"

# Revisions whose ``tools`` surface this server is compatible with. A client
# that requests one of these gets it echoed back from ``initialize``; any other
# request falls back to `LATEST_PROTOCOL_VERSION`. ``2025-03-26`` is excluded: it
# predates the ``title`` / ``outputSchema`` / ``structuredContent`` fields this
# server emits.
_SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18", LATEST_PROTOCOL_VERSION})

# The first "modern" revision: no `initialize` handshake, no protocol-level
# session. A client declares its version, identity and capabilities in `_meta`
# on every request, and the server answers each one independently.
MODERN_PROTOCOL_VERSION = "2026-07-28"

# Every revision this server serves, newest first. Ordering matters: it is
# echoed verbatim in `server/discover` and in an `UnsupportedProtocolVersion`
# error, and a client picks from the front.
SERVED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    MODERN_PROTOCOL_VERSION,
    LATEST_PROTOCOL_VERSION,
    "2025-06-18",
)

# `_meta` keys the modern revision reserves. Prefixed per the spec's naming
# rules, so an application's own `_meta` entries cannot collide with them.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
# The modern revision sets the log level per request rather than per connection. A
# request that omits it gets no `notifications/message` at all.
META_LOG_LEVEL = "io.modelcontextprotocol/logLevel"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# Every modern result carries this discriminator. `"complete"` is an ordinary
# result; `"input_required"` marks a multi-round-trip interim result, which this
# server does not yet produce.
RESULT_TYPE_COMPLETE = "complete"
RESULT_TYPE_TASK = "task"
# The methods the spec requires caching hints on, for a `complete` result.
# Methods a handshake-era client has and a modern one does not. Still served to the
# revision that defined them, reported as not found to the revision that removed
# them, so a client discovers the surface it actually has.
_HANDSHAKE_ONLY_METHODS = frozenset(
    {
        # Retired by the tasks extension.
        "tasks/list",
        "tasks/result",
        # Removed by the modern revision. A modern client sets its log level per
        # request in `_meta` instead of once per connection, and has no `ping`.
        "ping",
        "logging/setLevel",
    }
)
_CACHEABLE_METHODS = frozenset(
    {
        "server/discover",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
        "resources/read",
    }
)
# How long a client may consider a list result fresh. The registries are built once
# at startup and never change shape afterwards, so a generous default costs nothing
# in staleness and saves an agent re-listing on every reconnect.
DEFAULT_CACHE_TTL_MS = 300_000
# A result that can differ between callers must not be cached by a shared proxy.
_CACHE_SCOPE_PUBLIC = "public"
_CACHE_SCOPE_PRIVATE = "private"

# Membership set for the per-request version check; the tuple above is the
# ordered form clients are shown.
_SERVED_VERSION_SET = frozenset(SERVED_PROTOCOL_VERSIONS)


def _apply_sync_tool_filter(
    tool_filter: Any, tools: list[MCPTool], principal: Any
) -> list[MCPTool]:
    """Run a synchronous visibility policy over the whole candidate set."""
    return [tool for tool in tools if tool_filter(tool, principal)]


def _build_tool_listing_entry(tool: MCPTool) -> dict[str, Any]:
    """Shape one registered tool into its `tools/list` entry.

    Beyond the required `name` / `description` / `inputSchema`, a route-backed
    tool carries a human-readable `title` (its route summary), HTTP-derived
    `annotations` (read-only / idempotent / destructive hints), and an
    `outputSchema` when its result has a declared object shape.
    """
    entry: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }
    if tool.title:
        entry["title"] = tool.title
    icons = render_icons(tool.icons)
    if icons is not None:
        entry["icons"] = icons
    annotations = _tool_annotations(tool.route_methods, tool.title)
    if annotations is not None:
        entry["annotations"] = annotations
    if tool.output_schema is not None:
        entry["outputSchema"] = tool.output_schema
    # A tool that opts into background execution advertises it so a client
    # knows it may send a task-augmented `tools/call`. The spec's default is
    # `"forbidden"`, so a non-opting tool omits the field entirely.
    if tool.task_support:
        entry["execution"] = {"taskSupport": "optional"}
    return entry


class MCPServer(TasksMixin, InvocationMixin):
    """Serve a Veloce app's MCP tools over JSON-RPC 2.0.

    Build once with the app; the registry is assembled eagerly so a
    registration-time safety violation (missing description, duplicate name)
    surfaces before any client connects.
    """

    __slots__ = (
        "app",
        "prompts",
        "registry",
        "resources",
        "server_instructions",
        "server_name",
        "server_title",
        "server_version",
        "_call_timeout",
        "_enforce_lifecycle",
        "_tool_filter",
        "_cache_ttl_ms",
        "_any_scoped_tools",
        "_any_scoped_prompts",
        "_subscriptions_enabled",
        "_connections",
        "_capabilities",
        "_methods",
        "_inflight",
        "_tasks",
    )

    def __init__(
        self,
        app: Any,
        registry: ToolRegistry | None = None,
        resources: ResourceRegistry | None = None,
        prompts: PromptRegistry | None = None,
        tool_filter: ToolFilter | None = None,
        cache_ttl_ms: int = DEFAULT_CACHE_TTL_MS,
    ) -> None:
        self.app = app
        # Optional per-caller `tools/list` visibility policy. `None` - the default -
        # leaves listing unfiltered, so an application that does not opt in pays
        # nothing and sees exactly the pre-existing behaviour.
        self._tool_filter = tool_filter
        # Freshness hint sent with cacheable results. The spec requires `>= 0`;
        # zero tells the client to treat every result as immediately stale.
        self._cache_ttl_ms = max(0, cache_ttl_ms)
        self.registry = registry if registry is not None else build_registry(app)
        self.resources = resources if resources is not None else build_resource_registry(app)
        self.prompts = prompts if prompts is not None else build_prompt_registry(app)
        # Bind every `@app.mcp_completer` registration onto its prompt / resource
        # descriptor now the registries exist, so a misconfigured target surfaces
        # at build time and `CompletionsCapability` finds the completers in place.
        attach_completers(app, self.prompts, self.resources)
        # Whether either list can differ between two authorized callers, decided
        # once here: the registries are fixed after build, so the cache-scope
        # decision must not walk them per request.
        self._any_scoped_tools = any(tool.required_scopes for tool in self.registry.tools.values())
        self._any_scoped_prompts = any(
            prompt.tool.required_scopes for prompt in self.prompts.prompts.values()
        )
        self.server_name = getattr(app, "title", None) or "Veloce"
        self.server_version = getattr(app, "version", None) or "0.1.0"
        # Human-facing display name and client-facing usage guidance for the
        # `initialize` result, read from the same app metadata the OpenAPI
        # document uses so the two doors describe the server identically. The
        # title falls back to the identifier name; instructions prefer the longer
        # `description`, then the one-line `summary`. Empty when neither is set.
        self.server_title = getattr(app, "title", None) or None
        self.server_instructions = (
            getattr(app, "description", None) or getattr(app, "summary", None) or None
        )
        # Optional per-call wall-clock budget (`MCP_CALL_TIMEOUT` seconds in
        # `app.config`). The stdio serve loop is serial, so a handler that awaits
        # forever wedges every later call; when set, a call exceeding the budget
        # is cancelled and surfaced as an in-band tool error. `None` disables it.
        config = getattr(app, "config", None)
        self._call_timeout = config.get("MCP_CALL_TIMEOUT") if config is not None else None
        # Opt-in lifecycle ordering on a stateful connection (`MCP_ENFORCE_LIFECYCLE`
        # in `app.config`). When on, a stateful transport's session rejects any
        # request other than `initialize` / `ping` that precedes initialization
        # (the spec's "initialization MUST be first" rule). Off by default so the
        # existing stdio wire behavior is unchanged; the session still records the
        # client's advertised capabilities either way.
        self._enforce_lifecycle = bool(
            config.get("MCP_ENFORCE_LIFECYCLE") if config is not None else False
        )
        # Opt-in resource subscriptions (`MCP_RESOURCE_SUBSCRIPTIONS` in
        # `app.config`). When on, the resource capability advertises
        # `subscribe`/`listChanged`, the subscribe / unsubscribe methods are
        # served, and `notify_resource_updated` / `notify_resources_list_changed`
        # fan changes out to subscribed connections. Off by default so the
        # existing wire behavior and zero-overhead path are unchanged.
        self._subscriptions_enabled = bool(
            config.get("MCP_RESOURCE_SUBSCRIPTIONS") if config is not None else False
        )
        # The live stateful connections that may receive resource notifications;
        # built only when subscriptions are on, so the default path holds nothing.
        self._connections = ConnectionRegistry() if self._subscriptions_enabled else None
        # Background task store + capability for task-augmented tool calls. The
        # store is shared between `_tools_call` (which creates a task) and the
        # capability (which serves `tasks/get|result|list|cancel`); it holds no
        # task and costs nothing until a client opts a call into a task.
        self._tasks = TaskRegistry()
        # The spec areas this server serves, each owning its `initialize`
        # advertisement and its method handlers. A new area is a capability
        # added here, not a branch edited into the dispatcher or `_initialize`.
        capabilities: list[Capability] = [
            ToolsCapability(self),
            ResourcesCapability(self),
            PromptsCapability(self),
            CompletionsCapability(self),
            TasksCapability(self),
            LoggingCapability(self),
        ]
        # The subscribe / unsubscribe methods are registered only when the feature
        # is on, so an off server returns method-not-found for them (matching the
        # `subscribe: false` it advertises) and pays no dispatch-map cost.
        if self._subscriptions_enabled:
            capabilities.append(SubscriptionsCapability(self))
        self._capabilities: tuple[Capability, ...] = tuple(capabilities)
        # Built once at construction so per-request dispatch is one dict lookup.
        # A new method is registered here, never wired into a dispatcher branch.
        self._methods: dict[str, MethodHandler] = self._build_method_map()
        # Cancellable requests in flight, keyed by `(connection_key, msg_id)` so a
        # JSON-RPC id is unique only within its own connection. The client owns its
        # id space per connection, so two HTTP clients of the same server routinely
        # reuse id `1`; keying by id alone would let one client's
        # `notifications/cancelled` reach a peer's call. The connection key is the
        # dispatching session's identity (the stdio loop's one session, an HTTP
        # `Mcp-Session-Id`'s session, or a stateless POST's ephemeral session);
        # `None` only for a direct `handle_message` with no session. Populated only
        # for an id-bearing request and popped when it settles.
        self._inflight: dict[tuple[int | None, Any], _InFlight] = {}

    def _build_method_map(self) -> dict[str, MethodHandler]:
        """Map each supported JSON-RPC method to its async handler.

        The lifecycle methods (`initialize`, `ping`, the `initialized` ack) are
        core to every server; the spec-area methods are contributed by the held
        capabilities. Every entry shares the `async (params) -> result | None`
        shape `handle_message` dispatches.
        """
        methods: dict[str, MethodHandler] = {
            "initialize": self._handle_initialize,
            "notifications/initialized": self._handle_initialized,
            "notifications/cancelled": self._handle_cancelled,
            "ping": self._handle_ping,
            "server/discover": self._handle_discover,
        }
        for capability in self._capabilities:
            methods.update(capability.handlers())
        return methods

    @staticmethod
    def set_notifier(notifier: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Wire the current context's outbound one-way notification sink.

        Sets the per-request `_notifier_var`; the stdio transport calls this once
        in its serve task, while the Streamable HTTP transport sets the var per
        request so concurrent calls never cross notifications.
        """
        _notifier_var.set(notifier)

    @staticmethod
    def set_requester(
        requester: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        """Wire the current context's server->client request issuer.

        Sets the per-context `_requester_var`; a bidirectional transport (the stdio
        loop) calls this once in its serve task so a tool's `MCPContext.sample` /
        `elicit` / `roots` reaches the client. A one-way transport never calls it,
        leaving those methods to raise.
        """
        _requester_var.set(requester)

    @staticmethod
    def current_request_id() -> Any:
        """The JSON-RPC id of the request being dispatched, or None for a notification."""
        return _request_id_var.get()

    @staticmethod
    async def send_to_current_connection(message: dict[str, Any]) -> None:
        """Send one server-initiated message down the dispatching connection."""
        notifier = _notifier_var.get()
        if notifier is not None:
            await notifier(message)

    @staticmethod
    def current_session() -> MCPSession | None:
        """Return the session of the connection currently dispatching, or `None`.

        Set per dispatch by `handle_message` when a stateful transport supplies a
        session; `None` on the stateless HTTP path or off-dispatch.
        """
        return _session_var.get()

    # ── Resource subscriptions ────────────────────────────

    def register_connection(
        self, session: MCPSession, sink: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> object | None:
        """Record an open stateful connection so it can receive resource updates.

        Returns an opaque token a transport passes back to `unregister_connection`
        to drop exactly this stream, so concurrent streams on one session are
        tracked independently. A no-op returning `None` when subscriptions are
        disabled, so a transport may call this unconditionally.
        """
        if self._connections is not None:
            return self._connections.add(session, sink)
        return None

    def unregister_connection(self, token: object | None) -> None:
        """Drop the connection named by its token (a no-op when token is `None`).

        Any `subscriptions/listen` streams the connection held go with it: the
        transport is gone, so there is nowhere to send a graceful close, and a
        stream left registered would keep a dead session reachable by fan-out.
        """
        if self._connections is not None and token is not None:
            self._connections.forget_streams(token)
            self._connections.remove(token)

    def evict_session(self, session: MCPSession) -> None:
        """Reclaim everything an evicted session owns: its connection and tasks.

        Called when a session's transport drops it (idle TTL on HTTP). Beyond
        unregistering the subscription connection, this cancels and drops the
        session's tasks - including a never-settling one TTL eviction would leave
        in place - so an abandoned session cannot pin a task for the process
        lifetime.
        """
        if self._connections is not None:
            self._connections.remove_session(session)
        for task in self._tasks.owned_by(session.connection_id):
            if not task.is_terminal() and task.runner is not None and not task.runner.done():
                task.runner.cancel()
            self._tasks.drop(task)

    async def notify_resource_updated(self, uri: str) -> None:
        """Tell subscribed clients a resource changed (`notifications/resources/updated`).

        Call this from the app when a resource's data changes; the server fans the
        notification out to every connection subscribed to `uri`. A no-op when
        subscriptions are disabled or no connection subscribed to `uri`.
        """
        if self._connections is not None:
            await self._connections.notify_updated(uri)

    async def notify_resources_list_changed(self) -> None:
        """Tell clients the resource list changed (`notifications/resources/list_changed`).

        Call this from the app when the set of available resources changes; the
        server fans the notification out to every open connection. A no-op when
        subscriptions are disabled.
        """
        if self._connections is not None:
            await self._connections.notify_list_changed()

    async def notify_tools_list_changed(self) -> None:
        """Tell listening clients the tool list changed.

        Reaches only the `subscriptions/listen` streams that asked for
        `toolsListChanged`; the spec forbids sending a type a client did not
        request. A no-op when nothing is listening.
        """
        if self._connections is not None:
            await self._connections.notify_topic("toolsListChanged")

    async def notify_prompts_list_changed(self) -> None:
        """Tell listening clients the prompt list changed.

        Reaches only the streams that asked for `promptsListChanged`.
        """
        if self._connections is not None:
            await self._connections.notify_topic("promptsListChanged")

    # ── JSON-RPC dispatch ─────────────────────────────────

    async def handle_message(
        self, message: dict[str, Any], session: MCPSession | None = None
    ) -> dict[str, Any] | None:
        """Dispatch one decoded JSON-RPC request; return the response object.

        Returns `None` for a notification (a request with no ``id``), which
        carries no response per JSON-RPC 2.0 Sec. 4.1.

        A stateful transport (the serial stdio loop) passes its `session` so the
        server records the client's advertised capabilities from `initialize` and
        enforces the lifecycle ordering: before `initialize` completes the only
        requests answered are `initialize` and `ping`. The stateless HTTP
        transport passes none, leaving its fast path unaffected.
        """
        msg_id = message.get("id")
        method = message.get("method")

        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _error(msg_id, _JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")

        params = message.get("params") or {}
        is_notification = "id" not in message

        # Set by the era block below when a modern request names a log level, and
        # reset with the other per-message context in the `finally`.
        log_level_token = None

        # Era selection. A modern client states its version in `_meta` on every
        # request; a legacy one opens with `initialize` and negotiates once. The
        # two are served side by side, so the same endpoint answers both.
        meta = params.get("_meta") if isinstance(params, dict) else None
        raw_version = meta.get(META_PROTOCOL_VERSION) if isinstance(meta, dict) else None
        requested_version: str | None = raw_version if isinstance(raw_version, str) else None
        is_modern = requested_version is not None
        if is_modern and session is not None:
            # A modern client never sends `initialize`, so its identity and
            # capabilities arrive in `_meta` on every request instead. Recording
            # them on the session is what makes `MCPContext.client_info` /
            # `client_capabilities` answer, and what lets the server-initiated
            # requests (`sample` / `elicit` / `roots`) see the capabilities the
            # client actually advertised rather than an empty set. Both shipped
            # transports pass a session; a bare `handle_message` has none, and
            # then there is nowhere to record it.
            session.record_request_meta(meta)
        if is_modern:
            # Per request, not per connection: a modern request that names no level
            # receives no log notifications, which the spec requires.
            raw_level = meta.get(META_LOG_LEVEL) if isinstance(meta, dict) else None
            level = raw_level if isinstance(raw_level, str) and raw_level in _LOG_RANKS else None
            log_level_token = _log_level_var.set(level if level is not None else LOG_LEVEL_OFF)
        if requested_version is not None and requested_version not in _SERVED_VERSION_SET:
            # Recoverable by design: the client picks from `supported` and
            # retries. Returning this code is also what identifies the server as
            # modern, so a probing client stops falling back to `initialize`.
            if is_notification:
                return None
            return UnsupportedProtocolVersionError(
                requested_version, SERVED_PROTOCOL_VERSIONS
            ).to_error(msg_id)

        # On a stateful connection the initialization exchange MUST be first: a
        # request other than `initialize` / `ping` arriving before it completes is
        # rejected, and the client's advertised capabilities are recorded here.
        if session is not None:
            rejection = self._gate_session(session, method, params, msg_id, is_notification)
            if rejection is not None:
                return rejection

        handler = self._methods.get(method)
        if handler is not None and is_modern and method in _HANDSHAKE_ONLY_METHODS:
            # The tasks extension retired these; a modern client polls `tasks/get`,
            # whose result carries the completed answer. Reported as not found so a
            # client discovers the surface it actually has.
            handler = None
        if handler is None:
            # An unknown notification carries no response; an unknown request is a
            # method-not-found error.
            if is_notification:
                return None
            return _error(msg_id, _JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")

        # Register an id-bearing request as cancellable so a later
        # `notifications/cancelled` can reach its task; `initialize` is excluded
        # because the spec forbids cancelling it. A notification (no id) and the
        # zero-cancellation common case never touch the registry. The registry is
        # keyed per connection so one client cannot cancel a peer's colliding id.
        connection_key = session.connection_id if session is not None else None
        inflight_key = (connection_key, msg_id)
        inflight = self._track_inflight(inflight_key, method) if not is_notification else None
        token = _inflight_var.set(inflight)
        # Expose the connection's session so a per-connection method
        # (`resources/subscribe`) reaches the session it mutates; `None` on the
        # stateless path leaves the subscribe handler to reject the call.
        session_token = _session_var.set(session)
        request_id_token = _request_id_var.set(msg_id)
        try:
            result = await handler(params)
        except MCPError as exc:
            # Polymorphic: the JSON-RPC code and any `data` come from the subclass.
            return exc.to_error(msg_id)
        except asyncio.TimeoutError:
            # A resources/read or prompts/get that overran the per-call budget
            # (a tools/call surfaces its own timeout in-band before here).
            return _error(msg_id, _JSONRPC_INTERNAL_ERROR, "request exceeded the MCP call timeout")
        except Exception as exc:  # pragma: no cover - defensive
            _logger.exception("MCP method %s raised", method)
            return _error(msg_id, _JSONRPC_INTERNAL_ERROR, self._error_text(exc, "internal error"))
        finally:
            _inflight_var.reset(token)
            _session_var.reset(session_token)
            _request_id_var.reset(request_id_token)
            if log_level_token is not None:
                _log_level_var.reset(log_level_token)
            if inflight is not None:
                self._inflight.pop(inflight_key, None)

        if is_notification:
            return None
        if result is DEFERRED_RESPONSE:
            # A long-lived request answered by its own closure, not here.
            return None
        if is_modern and isinstance(result, dict) and "resultType" in result:
            pass
        elif is_modern and isinstance(result, dict) and "task" in result:
            # A task handle is its own result type; the client polls rather than
            # reading a completed answer here.
            result = {"resultType": RESULT_TYPE_TASK, **result}
        elif is_modern and isinstance(result, dict):
            # Required on every modern result. A legacy client must never see it:
            # its revision has no such field and the server info below belongs to
            # the modern shape only.
            result = {"resultType": RESULT_TYPE_COMPLETE, **result}
            if method in _CACHEABLE_METHODS:
                self._add_cache_hints(method, result)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _gate_session(
        self,
        session: MCPSession,
        method: str,
        params: dict[str, Any],
        msg_id: Any,
        is_notification: bool,
    ) -> dict[str, Any] | None:
        """Record client capabilities on a session and optionally enforce ordering.

        Records the client's advertised capabilities from `initialize` and marks
        the session initialized on the `notifications/initialized` ack - always,
        so the recorded state is available regardless of the ordering policy. When
        `MCP_ENFORCE_LIFECYCLE` is on, any request other than `initialize` / `ping`
        that precedes initialization is rejected with an invalid-request error;
        notifications always pass (the spec orders requests, not one-way messages).
        Returns the JSON-RPC error to send back, or `None` to proceed.
        """
        if method == "initialize":
            session.record_initialize(params)
            return None
        if method == "notifications/initialized":
            session.initialized = True
            return None
        # Lifecycle ordering is a property of a persistent connection; a stateless
        # per-request session is never initialized across messages, so enforcing it
        # there would reject every independent POST.
        if not self._enforce_lifecycle or not session.persistent:
            return None
        if session.initialized or is_notification or method == "ping":
            return None
        return _error(
            msg_id,
            _JSONRPC_INVALID_REQUEST,
            f"Received {method!r} before initialization completed",
        )

    def _track_inflight(self, key: tuple[int | None, Any], method: str) -> _InFlight | None:
        """Register an in-flight request so a cancel notification can reach it.

        Skips `initialize` (the spec forbids cancelling it) and any request not
        running inside a task (a bare synchronous driver). Returns the holder, or
        `None` when the request is not tracked. `key` is `(connection_key, msg_id)`
        so a cancel from one connection cannot reach another's colliding id.
        """
        if method == "initialize":
            return None
        task = asyncio.current_task()
        if task is None:
            return None
        holder = _InFlight(task)
        self._inflight[key] = holder
        return holder

    async def close_listen_stream(self, session: MCPSession, subscription_id: Any) -> None:
        """End one open stream, answering its long-lived request as it closes.

        The response is what tells the client the subscription ended cleanly, as
        opposed to a transport that simply dropped.
        """
        if session.listen_streams.pop(subscription_id, None) is None:
            return
        await self.send_to_current_connection(subscription_closed_response(subscription_id))

    async def _handle_cancelled(self, params: dict[str, Any]) -> None:
        """Cancel the request named by ``notifications/cancelled`` (a notification).

        Resolves the ``requestId`` within the cancelling connection's own id space
        and cancels its task, marking the call's context cancelled. An unknown id is
        ignored: the request may have already completed, which the spec expects
        clients to race. The lookup is scoped to the dispatching connection so a
        client can cancel only its own in-flight request, never a peer's whose
        JSON-RPC id happens to collide.
        """
        request_id = params.get("requestId")
        # A JSON-RPC id is a string or a number; a list/object would make the
        # `(connection_key, request_id)` lookup key unhashable. A malformed id
        # matches no in-flight request, so ignore the notification.
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            return None
        session = _session_var.get()
        # A `subscriptions/listen` is never in-flight - it returned as soon as it
        # opened the stream - so cancelling one means ending its stream. On stdio
        # this notification is the only way a client closes a subscription.
        if session is not None and request_id in session.listen_streams:
            await self.close_listen_stream(session, request_id)
            return None
        connection_key = session.connection_id if session is not None else None
        holder = self._inflight.get((connection_key, request_id))
        if holder is not None:
            holder.cancel()
        return None

    # ── Dispatch adapters ─────────────────────────────────
    #
    # Thin async wrappers giving every dispatch-map entry the uniform
    # `async (params) -> result | None` shape. The sync implementations they call
    # stay focused on building their result; `tools/call`, `resources/read`, and
    # `prompts/get` are already async + params-shaped and registered directly.

    async def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._initialize(params)

    async def _handle_initialized(self, params: dict[str, Any]) -> None:
        # Client handshake ack - a notification, no response.
        return None

    async def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        # Base liveness utility either side may send; the spec'd reply is an empty
        # result object.
        return {}

    async def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        # Unfiltered is the default and stays a plain synchronous build - no awaits,
        # no per-tool predicate - so opting out costs nothing.
        if self._tool_filter is None:
            return self._tools_list()
        return self._describe_tools(await self._visible_tools())

    async def _handle_resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._resources_list()

    async def _handle_resource_templates_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._resource_templates_list()

    async def _handle_prompts_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._prompts_list()

    async def _handle_set_log_level(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._set_log_level(params)

    # ── Method handlers ───────────────────────────────────

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        # Echo the client's requested revision when supported; otherwise return
        # the server's latest, leaving the client to decide whether to proceed.
        requested = params.get("protocolVersion")
        version = (
            requested
            if isinstance(requested, str) and requested in _SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        capabilities = self._advertised_capabilities()
        server_info = self._server_info()
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": capabilities,
            "serverInfo": server_info,
        }
        # `instructions` is optional usage guidance the client may surface to its
        # model; the app's `description` (falling back to its `summary`) is that
        # guidance, derived from the same contract that documents the HTTP API.
        if self.server_instructions:
            result["instructions"] = self.server_instructions
        return result

    def _advertised_capabilities(self) -> dict[str, Any]:
        """Collect every held capability's advertisement.

        A `None` advertisement is dropped so a client never probes a primitive
        the app does not expose.
        """
        capabilities: dict[str, Any] = {}
        for capability in self._capabilities:
            entry = capability.advertise()
            if entry is not None:
                capabilities.update(entry)
        return capabilities

    def _server_info(self) -> dict[str, Any]:
        """Identity block shared by `initialize` and `server/discover`.

        `title` is the human-facing display name; `name` / `version` carry the
        identifier and version.
        """
        info: dict[str, Any] = {"name": self.server_name, "version": self.server_version}
        if self.server_title:
            info["title"] = self.server_title
        return info

    async def _handle_discover(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer `server/discover` with the versions, capabilities and identity.

        The modern revision replaces the `initialize` handshake with this single
        probe, and requires servers to implement it. `supportedVersions` is the
        load-bearing field: a client picks one and declares it on every later
        request. It is ordered newest-first so a client taking the head gets the
        newest revision both sides serve.

        `serverInfo` travels in `_meta` here rather than as a top-level field,
        which is where the modern revision moved it.
        """
        capabilities = self._advertised_capabilities()
        extensions = self._advertised_extensions()
        if extensions:
            capabilities = {**capabilities, "extensions": extensions}
        return {
            "supportedVersions": list(SERVED_PROTOCOL_VERSIONS),
            "capabilities": capabilities,
            "_meta": {META_SERVER_INFO: self._server_info()},
            **({"instructions": self.server_instructions} if self.server_instructions else {}),
        }

    def _advertised_extensions(self) -> dict[str, Any]:
        """The protocol extensions this server implements, for `server/discover`.

        A capability contributes an entry only when the feature it names is
        actually available, so a server with no task-capable tool advertises no
        tasks extension and a client will never offer one.
        """
        advertised: dict[str, Any] = {}
        for capability in self._capabilities:
            contributed = getattr(capability, "extensions", None)
            if contributed is None:
                continue
            entry = contributed()
            if entry:
                advertised.update(entry)
        return advertised

    def _tools_list(self) -> dict[str, Any]:
        return self._describe_tools(self.registry.tools.values())

    def _add_cache_hints(self, method: str, result: dict[str, Any]) -> None:
        """Attach `ttlMs` / `cacheScope` to a cacheable `complete` result.

        The scope is the load-bearing half. A result that can differ between
        callers - a `tools/list` narrowed by a visibility policy or by declared
        scopes, or a `resources/read` whose route authorizes per principal - is
        `private`, so a shared gateway cannot serve one caller's answer to another.
        Everything else is `public`. The spec is explicit that `cacheScope` is a
        hint and never a substitute for the per-primitive access control that
        already runs on each of these paths.
        """
        result["ttlMs"] = self._cache_ttl_ms
        result["cacheScope"] = (
            _CACHE_SCOPE_PRIVATE if self._varies_by_caller(method) else _CACHE_SCOPE_PUBLIC
        )

    def _varies_by_caller(self, method: str) -> bool:
        """Whether this method's result can differ between two authorized callers."""
        if method == "resources/read":
            # Its route runs the full request lifecycle under the caller's
            # principal, so the body it returns is caller-dependent by construction.
            return True
        if method == "tools/list":
            return self._tool_filter is not None or self._any_scoped_tools
        if method == "prompts/list":
            return self._any_scoped_prompts
        return False

    def _describe_tools(self, tools: Iterable[MCPTool]) -> dict[str, Any]:
        """Shape an already-selected sequence of tools into a `tools/list` result."""
        return {"tools": [self._describe_tool(tool) for tool in tools]}

    async def _visible_tools(self) -> list[MCPTool]:
        """The tools the calling principal may see, in registration order.

        Visibility is scoped by the *same* check `tools/call` performs, so a tool is
        never listed for a caller that cannot invoke it. A configured filter narrows
        that set further; it can hide a tool, never reveal one the scope check
        rejected. Hiding a tool does not change what happens if it is called anyway -
        an unlisted tool still raises `AuthorizationError` - so a visibility policy
        can never be mistaken for the authorization decision.

        The spec permits exactly this axis: the tool set "MAY vary by the
        authorization presented on the request [...] since credentials are
        per-request input, not connection state". It is evaluated per request and
        never memoized - the framework cannot know when a principal's grants change,
        and `tools/list` is a session-start call rather than a hot path.
        """
        principal = current_principal()
        scoped = [
            tool
            for tool in self.registry.tools.values()
            if not _principal_lacks_scopes(tool.required_scopes)
        ]
        tool_filter = self._tool_filter
        if tool_filter is None:
            return scoped
        if _is_async_callable(tool_filter):
            visible = []
            for tool in scoped:
                # `_is_async_callable` establishes the awaitable branch; the alias
                # is a union of both call shapes, which the checker cannot narrow.
                if await cast("Awaitable[bool]", tool_filter(tool, principal)):
                    visible.append(tool)
            return visible
        # A sync policy may consult a database, so it is kept off the event loop -
        # but as one handoff for the whole pass, not one per tool. Offloading each
        # predicate individually turns a microsecond scan into milliseconds.
        return cast(
            "list[MCPTool]",
            await offload(_apply_sync_tool_filter, tool_filter, scoped, principal),
        )

    @staticmethod
    def _describe_tool(tool: MCPTool) -> dict[str, Any]:
        """Return this tool's `tools/list` entry, building it once per tool.

        The entry is memoized on the tool because it is a pure function of
        registration data: nothing it reads can change once the registry is
        built, and it holds nothing caller- or revision-specific. Callers treat
        the returned mapping as read-only - the dispatcher stamps `ttlMs` /
        `cacheScope` onto the enclosing result, never onto an entry.
        """
        entry = tool.listing_entry
        if entry is None:
            entry = tool.listing_entry = _build_tool_listing_entry(tool)
        return entry

    async def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise InvalidParamsError("tools/call requires a string 'name'")
        tool = self.registry.get(name)
        if tool is None:
            raise InvalidParamsError(f"Unknown tool: {name}")

        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise InvalidParamsError("tools/call 'arguments' must be an object")

        started = time.perf_counter()
        # An authorization failure is a protocol-level forbidden error (uniform
        # across tools, resources, and prompts), not a normal tool result.
        if _principal_lacks_scopes(tool.required_scopes):
            await self._instrument(tool, started, status.HTTP_403_FORBIDDEN)
            raise AuthorizationError(tool.required_scopes)

        # A task-augmented call (a `task` field naming the client's intent to run
        # it as a background task) is started detached: the tool must opt in, and
        # a `CreateTaskResult` is returned immediately instead of the synchronous
        # tool result, which the client retrieves later via `tasks/result`.
        if "task" in params:
            return self._create_task(tool, arguments, params)

        return await self._produce_tool_result(tool, arguments, started, _progress_token(params))

    async def _produce_tool_result(
        self,
        tool: MCPTool,
        arguments: dict[str, Any],
        started: float,
        progress_token: str | int | None,
    ) -> dict[str, Any]:
        """Invoke a tool and shape its return into the `tools/call` result object.

        Shared by the synchronous `tools/call` and the background task runner so
        a tool invoked either way runs the same handler dispatch and produces the
        same result shape (one handler, two doors). Authorization is checked by
        the caller; instrumentation and in-band error shaping happen here so both
        callers report a call's real outcome identically.
        """
        try:
            result = await self._run_invoke(tool, arguments, progress_token)
        except InvalidParamsError:
            raise
        except _InvalidArgumentsError as exc:
            # Surfaced verbatim, unlike a handler exception: the message names
            # the offending argument and what was expected, both derived from
            # the tool's own declared schema and the caller's own input, so
            # there is nothing to redact. Redacting it would leave the model
            # with "internal error" and nothing to correct - the opposite of
            # what an execution error is for.
            await self._instrument(tool, started, status.HTTP_422_UNPROCESSABLE_ENTITY)
            return _text_result(str(exc), is_error=True)
        except asyncio.TimeoutError:
            await self._instrument(tool, started, status.HTTP_504_GATEWAY_TIMEOUT)
            return _text_result(
                f"tool call exceeded the {self._call_timeout}s timeout", is_error=True
            )
        except AuthorizationError:
            # A tool reading a scoped resource or prompt through its `MCPContext`
            # hits the same check a direct `resources/read` would. That failure is a
            # protocol-level forbidden error wherever it arises - the same treatment
            # a tool lacking its own scopes gets above - so it must not be flattened
            # into an in-band "internal error" that tells the model nothing.
            await self._instrument(tool, started, status.HTTP_403_FORBIDDEN)
            raise
        except Exception as exc:
            # A pure `@app.mcp_tool` (no route) has no exception-handler
            # machinery to run through, so its handler error is surfaced
            # in-band (isError=true) rather than as a JSON-RPC transport error,
            # letting the agent read the message. A route-backed tool never
            # reaches here on a handler error: `_invoke` routes that exception
            # through the app's exception handlers and returns a `_RouteResponse`.
            # An unhandled handler error is a 500, recorded as such.
            await self._instrument(tool, started, status.HTTP_500_INTERNAL_SERVER_ERROR)
            return _text_result(
                self._error_text(exc, "the tool raised an internal error"), is_error=True
            )

        # A `before_request` / middleware short-circuit or a route-backed tool's
        # final `Response` carries the real status code (an auth 401, a 500 from
        # an exception handler, a 200 success); instrumentation must report that,
        # not a hard-coded 200. The shaped result is derived from the same
        # response.
        if isinstance(result, (_ShortCircuit, _RouteResponse)):
            response = result.response
            try:
                await self._drain_stream(response)
            except _InBandError as exc:
                await self._instrument(tool, started, status.HTTP_500_INTERNAL_SERVER_ERROR)
                return _text_result(str(exc), is_error=True)
            await self._instrument(tool, started, response.status_code)
            # A `before_request` / middleware short-circuit response never went
            # through `response_model`; only a `_RouteResponse` carries the flag.
            model_filtered = isinstance(result, _RouteResponse) and result.model_filtered
            return self._result_from_response(tool, response, model_filtered)

        try:
            # A pure tool may return a streaming `Response`; buffer it so its
            # body becomes the tool result, then shape it like any buffered
            # return. `_drain_stream` is a no-op for a non-streaming value.
            if isinstance(result, Response):
                await self._drain_stream(result)
                # An image/audio Response has no text form; return the typed
                # content block directly, reporting the response's own status.
                binary = _binary_result(result)
                if binary is not None:
                    await self._instrument(tool, started, result.status_code)
                    return binary
            shaped = self._shape_result(tool, result)
        except _InBandError as exc:
            await self._instrument(tool, started, status.HTTP_500_INTERNAL_SERVER_ERROR)
            return _text_result(str(exc), is_error=True)
        # A pure tool's raw return that completed without error is a genuine 200.
        await self._instrument(tool, started, status.HTTP_200_OK)
        # A pure tool's `output_schema` is advertised from its declared return
        # type, but nothing on the pure path guarantees the handler actually
        # returned that type. Validate / coerce the raw return through the
        # declared model so the emitted `structuredContent` conforms to the
        # advertised schema (the MCP MUST). A value that cannot be coerced to the
        # schema's object shape is an in-band error, not a non-conforming result.
        if tool.output_model is not None:
            try:
                shaped = tool.output_model.model_validate(shaped).model_dump(mode="json")
            except Exception:
                return _text_result(
                    "tool result does not conform to the declared output schema", is_error=True
                )
        return self._success_result(tool, shaped)

    # ── Resources ─────────────────────────────────────────

    def _resources_list(self) -> dict[str, Any]:
        return {"resources": [_describe_resource(r) for r in self.resources.statics()]}

    def _resource_templates_list(self) -> dict[str, Any]:
        return {
            "resourceTemplates": [
                _describe_resource_template(r) for r in self.resources.templates()
            ]
        }

    async def _resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read one resource by URI, replaying its route through `_invoke`.

        The URI is matched against the registry (a static URI exactly, a template
        by its compiled pattern), the route's path-parameter values are recovered
        from the URI, and the handler runs through the same request lifecycle a
        tool call replays. The response body becomes the resource contents: a
        JSON/`text/*` body as `text`, any other media type as a base64 `blob`. An
        unknown URI - or a route answering 404 - is a resource-not-found error; a
        handler 4xx/5xx surfaces as a JSON-RPC error, since a resource read has no
        in-band error channel.
        """
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise InvalidParamsError("resources/read requires a string 'uri'")
        matched = self.resources.match(uri)
        if matched is None:
            raise ResourceNotFoundError(f"Unknown resource: {uri}")
        resource, arguments = matched
        if _principal_lacks_scopes(resource.tool.required_scopes):
            raise AuthorizationError(resource.tool.required_scopes)

        # A path-parameter value the URI carries that the route cannot coerce (a
        # non-int `{user_id}`) raises `InvalidParamsError`, which already maps to
        # the invalid-params code - it propagates unchanged.
        result = await self._run_invoke(resource.tool, arguments, _progress_token(params))

        # A resource is always route-backed, so `_invoke` yields a
        # `_RouteResponse` (or a `_ShortCircuit` from a middleware / before_request
        # guard); both carry the `Response` whose body is the resource contents.
        response = result.response if isinstance(result, (_ShortCircuit, _RouteResponse)) else None
        if response is None:
            raise InternalError(f"Resource {uri} produced no response")
        try:
            await self._drain_stream(response)
        except _InBandError as exc:
            raise InternalError(str(exc)) from exc
        if response.status_code >= 400:
            body = _stringify(_response_body_value(response))
            if response.status_code == status.HTTP_404_NOT_FOUND:
                raise ResourceNotFoundError(body)
            if response.status_code in (
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN,
            ):
                raise _ForbiddenError(body)
            raise InternalError(body)
        return {"contents": [_resource_contents(uri, response)]}

    # ── Prompts ───────────────────────────────────────────

    def _prompts_list(self) -> dict[str, Any]:
        return {"prompts": [_describe_prompt(p) for p in self.prompts.prompts.values()]}

    async def _prompts_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Render one prompt by name, replaying its callable through `_invoke`.

        The callable runs through the same pure-tool invocation path (DI graph,
        `MCPContext`, teardowns), and its return - a string or a list of
        role/content messages - is normalised into the MCP messages
        ``prompts/get`` returns. An unknown name or a malformed argument is an
        invalid-params error.
        """
        name = params.get("name")
        if not isinstance(name, str):
            raise InvalidParamsError("prompts/get requires a string 'name'")
        prompt = self.prompts.get(name)
        if prompt is None:
            raise InvalidParamsError(f"Unknown prompt: {name}")
        if _principal_lacks_scopes(prompt.tool.required_scopes):
            raise AuthorizationError(prompt.tool.required_scopes)
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise InvalidParamsError("prompts/get 'arguments' must be an object")

        result = await self._run_invoke(prompt.tool, arguments, _progress_token(params))
        out: dict[str, Any] = {"messages": _normalize_prompt_messages(result)}
        if prompt.description:
            out["description"] = prompt.description
        return out

    # ── Logging ───────────────────────────────────────────

    def _set_log_level(self, params: dict[str, Any]) -> dict[str, Any]:
        """Set the minimum level for ``notifications/message`` (logging/setLevel)."""
        level = params.get("level")
        if not isinstance(level, str) or level not in _LOG_RANKS:
            raise InvalidParamsError("logging/setLevel requires a valid RFC 5424 'level'")
        _log_level_var.set(level)
        return {}

    def _error_text(self, exc: Exception, generic: str) -> str:
        """Error text for an in-band / internal error payload, gated by debug.

        A pure `@app.mcp_tool` (and the defensive internal-error path) does not run
        through the app's exception handlers, so an exception's raw message could
        carry a secret - a DSN, a token, a path. With `app.debug` off a generic
        message is surfaced instead; with debug on the real message aids
        development. A route-backed tool is unaffected: its exceptions already go
        through `handle_user_exception`, which gates the body on debug itself.
        """
        return str(exc) if getattr(self.app, "debug", False) else generic
