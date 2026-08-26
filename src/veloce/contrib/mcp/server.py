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
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from veloce import status
from veloce._internal import _is_async_callable, offload
from veloce._model_backend import shape_through_model
from veloce.contrib.mcp._helpers import (
    _DEFERRED_RESPONSE,
    _attach_result_meta,
    _binary_result,
    _declared_mime_type,
    _describe_prompt,
    _describe_resource,
    _describe_resource_template,
    _era_modern_var,
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
from veloce.contrib.mcp.context import _LOG_RANKS, LOG_LEVEL_OFF, _result_meta_var, _session_var
from veloce.contrib.mcp.errors import (
    _JSONRPC_INTERNAL_ERROR,
    _JSONRPC_INVALID_REQUEST,
    _JSONRPC_METHOD_NOT_FOUND,
    AuthorizationError,
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    MCPCapabilityError,
    MCPError,
    ResourceNotFoundError,
    UnsupportedProtocolVersionError,
    _error,
    _ForbiddenError,
    _InBandError,
    _InvalidArgumentsError,
)
from veloce.contrib.mcp.icons import coerce_icons, render_icons
from veloce.contrib.mcp.pagination import paginate
from veloce.contrib.mcp.prompts import PromptRegistry, build_prompt_registry
from veloce.contrib.mcp.registry import (
    VERSION_META_KEY,
    ToolFilter,
    ToolRegistry,
    build_registry,
)
from veloce.contrib.mcp.resources import ResourceRegistry, build_resource_registry
from veloce.contrib.mcp.subscriptions import (
    ConnectionRegistry,
    SubscriptionsCapability,
    subscription_closed_response,
)
from veloce.contrib.mcp.tasks import TaskRegistry, TasksCapability
from veloce.contrib.mcp.toolsearch import ToolSearch
from veloce.http.response import Response
from veloce.json_provider import resolve_dumps
from veloce.principal import current_principal

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.prompts import MCPPrompt
    from veloce.contrib.mcp.registry import MCPTool
    from veloce.contrib.mcp.resources import MCPResource
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
#: The revision before `LATEST_PROTOCOL_VERSION`, still served.
PRIOR_PROTOCOL_VERSION = "2025-06-18"

_SUPPORTED_PROTOCOL_VERSIONS = frozenset({PRIOR_PROTOCOL_VERSION, LATEST_PROTOCOL_VERSION})

# The first "modern" revision: no `initialize` handshake, no protocol-level
# session. A client declares its version, identity and capabilities in `_meta`
# on every request, and the server answers each one independently.
MODERN_PROTOCOL_VERSION = "2026-07-28"


def is_modern_version(version: str | None) -> bool:
    """Whether `version` names a revision served in the modern shape.

    The revisions are ISO dates, so ordering them as strings orders them by date
    and a revision later than the first modern one is modern too.

    The one definition both ends use. Deciding the era twice - the transport on
    the value, the core on the mere presence of a `_meta` version - let a body
    naming a handshake-era revision skip the transport's header cross-check
    while the core answered it in the modern envelope. The cross-check exists so
    a hop's two ends cannot act on different requests; the server's own two ends
    did.
    """
    return version is not None and version >= MODERN_PROTOCOL_VERSION


# Every revision this server serves, newest first. Ordering matters: it is
# echoed verbatim in `server/discover` and in an `UnsupportedProtocolVersion`
# error, and a client picks from the front.
SERVED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    MODERN_PROTOCOL_VERSION,
    LATEST_PROTOCOL_VERSION,
    PRIOR_PROTOCOL_VERSION,
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
# `ping` belongs to no capability - it is answered by the server itself - so its
# retirement is declared here. Every other era-retired method is declared by the
# capability that owns it, as `Capability.handshake_only_methods`, and the
# effective set is their union (see `_handshake_only_methods`).
_CORE_HANDSHAKE_ONLY_METHODS = frozenset({"ping"})
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

# A prompt or a resource: both are gated by the scopes of the `MCPTool` they
# carry, so one narrowing walk serves either listing.
_ScopedT = TypeVar("_ScopedT", bound="MCPPrompt | MCPResource")


# A catalogue is paged by the same key its registry indexes it under, so a
# cursor names the item the way the registry itself does.
def _tool_key(tool: MCPTool) -> str:
    return tool.name


def _resource_key(resource: MCPResource) -> str:
    return resource.uri


def _prompt_key(prompt: MCPPrompt) -> str:
    return prompt.name


def _apply_sync_tool_filter(
    tool_filter: Any, tools: list[MCPTool], principal: Any
) -> list[MCPTool]:
    """Run a synchronous visibility policy over the whole candidate set."""
    return [tool for tool in tools if tool_filter(tool, principal)]


def _requested_version(params: dict[str, Any]) -> str | None:
    """Return the tool version this call asked for, if it named one."""
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    namespaced = meta.get(VERSION_META_KEY)
    if not isinstance(namespaced, dict):
        return None
    version = namespaced.get("version")
    return version if isinstance(version, str) else None


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
    # Derived from the route's verb, then overlaid with whatever the author
    # declared - so a tool can correct a hint the verb implies, and a tool with no
    # verb can state hints of its own.
    annotations = _tool_annotations(tool.route_methods, tool.title)
    if tool.annotations:
        annotations = {**(annotations or {}), **tool.annotations}
    if annotations:
        entry["annotations"] = annotations
    if tool.output_schema is not None:
        entry["outputSchema"] = tool.output_schema
    # A tool that opts into background execution advertises it so a client
    # knows it may send a task-augmented `tools/call`. The spec's default is
    # `"forbidden"`, so a non-opting tool omits the field entirely.
    if tool.task_support:
        entry["execution"] = {"taskSupport": "optional"}
    meta = tool.meta
    if tool.version is not None:
        published: dict[str, Any] = {"version": tool.version}
        if tool.version_history:
            published["versions"] = list(tool.version_history)
        declared = meta.get(VERSION_META_KEY) if meta else None
        namespaced = {**declared, **published} if isinstance(declared, dict) else published
        meta = {**meta, VERSION_META_KEY: namespaced} if meta else {VERSION_META_KEY: namespaced}
    if meta:
        entry["_meta"] = meta
    return entry


# The methods whose answer is a listing, and so can be narrowed per connection.
_LIST_METHODS = frozenset(
    {"tools/list", "prompts/list", "resources/list", "resources/templates/list"}
)


def _in_band_status_for(error: _InBandError) -> int:
    """Map an in-band failure to the status instrumentation should record."""
    if isinstance(error, _InvalidArgumentsError):
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    if isinstance(error, MCPCapabilityError):
        # The call could not be completed because something it depended on - a
        # capability of the connected client - was not there to depend on.
        return status.HTTP_424_FAILED_DEPENDENCY
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _http_status_for(error: MCPError) -> int:
    """Map a raised MCP error to the status instrumentation should record.

    Instrumentation reports what happened in HTTP terms so one dashboard covers
    both doors; the JSON-RPC code the client receives is the error's own.
    """
    if isinstance(error, AuthorizationError):
        return status.HTTP_403_FORBIDDEN
    if isinstance(error, ResourceNotFoundError):
        return status.HTTP_404_NOT_FOUND
    if isinstance(error, InvalidParamsError):
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    if isinstance(error, InvalidRequestError):
        return status.HTTP_400_BAD_REQUEST
    return status.HTTP_500_INTERNAL_SERVER_ERROR


class _ServerPageSize:
    """Sentinel type: "use the server's configured page size".

    A distinct type rather than a bare `object()` so the page-size parameter
    narrows to `int | None` for the type checker once the sentinel is ruled out,
    and so an explicit `page_size=None` - meaning "the whole catalogue, unpaged"
    - stays distinguishable from "the caller said nothing".
    """

    __slots__ = ()


_SERVER_PAGE_SIZE = _ServerPageSize()


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
        "server_icons",
        "server_website_url",
        "_call_timeout",
        "_enforce_lifecycle",
        "_tool_filter",
        "_tool_search",
        "_cache_ttl_ms",
        "_page_size",
        "_any_scoped_tools",
        "_any_scoped_prompts",
        "_any_scoped_resources",
        "_subscriptions_enabled",
        "_connections",
        "_capabilities",
        "_result_dumps",
        "_handshake_only",
        "_era_aware_capabilities",
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
        page_size: int | None = None,
        tool_search: bool = False,
    ) -> None:
        self.app = app
        # Optional per-caller `tools/list` visibility policy. `None` - the default -
        # leaves listing unfiltered, so an application that does not opt in pays
        # nothing and sees exactly the pre-existing behaviour.
        self._tool_filter = tool_filter
        # Freshness hint sent with cacheable results. The spec requires `>= 0`;
        # zero tells the client to treat every result as immediately stale.
        self._cache_ttl_ms = max(0, cache_ttl_ms)
        # Optional page size for the list methods. `None` - the default - answers
        # every list in full, exactly as before: a client may ignore `nextCursor`,
        # so a server that paginated on its own would hide the rest of its
        # catalogue from every client that does.
        if page_size is not None and page_size < 1:
            raise ValueError(f"page_size must be a positive integer or None, got {page_size!r}")
        self._page_size = page_size
        self.registry = registry if registry is not None else build_registry(app)
        self.resources = resources if resources is not None else build_resource_registry(app)
        self.prompts = prompts if prompts is not None else build_prompt_registry(app)
        # Bind every `@app.mcp_completer` registration onto its prompt / resource
        # descriptor now the registries exist, so a misconfigured target surfaces
        # at build time and `CompletionsCapability` finds the completers in place.
        attach_completers(app, self.prompts, self.resources)
        # Set before anything reads it; built at the end of this constructor, once
        # there is a whole server for it to hold.
        self._tool_search: ToolSearch | None = None
        # Whether each list can differ between two callers, decided once here: the
        # registries are fixed after build, so neither the narrowing decision nor
        # the cache-scope decision may walk them per request. A server declaring
        # no scopes anywhere leaves every list on its plain synchronous build.
        self._any_scoped_tools = any(tool.required_scopes for tool in self.registry.tools.values())
        self._any_scoped_prompts = any(
            prompt.tool.required_scopes for prompt in self.prompts.prompts.values()
        )
        self._any_scoped_resources = any(
            resource.tool.required_scopes for resource in self.resources.resources.values()
        )
        # Read directly for the same reason `_build_info_object` does: the
        # constructor guarantees both, so a fallback here is a duplicated default.
        self.server_name = app.title
        self.server_version = app.version
        # Human-facing display name and client-facing usage guidance for the
        # `initialize` result, read from the same app metadata the OpenAPI
        # document uses so the two doors describe the server identically. The
        # spec separates the identifier `name` from the display `title`; an app
        # declares one title and it serves as both. Instructions prefer the
        # longer `description`, then the one-line `summary`, and are omitted when
        # neither is set.
        # The application's serialiser, resolved once. `resolve_dumps` returns
        # `None` when nothing is configured and the direct encoder already emits
        # the same bytes, so an app with no dialect pays nothing per tool call.
        self._result_dumps = resolve_dumps(app)
        self.server_title = app.title
        # Identity the spec lets a server publish about itself beyond its name:
        # icons a client can render beside it, and a page describing it. Both come
        # from the app, so one server identity is declared in one place.
        self.server_icons = coerce_icons(app.mcp_icons)
        self.server_website_url = app.website_url or None
        self.server_instructions = app.description or app.summary or None
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
        # Which capabilities vary their advertisement by protocol revision,
        # resolved once here rather than inspected on every handshake.
        self._era_aware_capabilities: frozenset[Capability] = frozenset(
            capability
            for capability in self._capabilities
            if "modern" in inspect.signature(capability.advertise).parameters
        )
        # Built once at construction so per-request dispatch is one dict lookup.
        # A new method is registered here, never wired into a dispatcher branch.
        self._methods: dict[str, MethodHandler] = self._build_method_map()
        # The era-retired methods, gathered from the capabilities that own them
        # exactly as the method map is. One rule, one place to edit.
        self._handshake_only: frozenset[str] = _CORE_HANDSHAKE_ONLY_METHODS.union(
            *(capability.handshake_only_methods for capability in self._capabilities)
        )
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

        # Optional search-first catalogue, built last: it holds this server and
        # calls back into it, so it is handed a constructed one rather than a
        # half-filled `self`. It registers its three tools into the registry,
        # after the scope scan above has already run - so the scan is redone
        # over the registry it leaves behind. The search tools declare no scopes
        # today and the answer is unchanged, but recomputing means a later
        # scoped search tool cannot silently skip the narrowing decision.
        if tool_search:
            self._tool_search = ToolSearch(self)
            self._any_scoped_tools = any(
                tool.required_scopes for tool in self.registry.tools.values()
            )

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

        if message.get("jsonrpc") != "2.0":
            return _error(msg_id, _JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")
        if not isinstance(method, str):
            # A reply to a server->client request carries an id and a result or an
            # error instead of a method. It is an answer, not a request: it needs
            # no response of its own, so it is accepted and dispatch stops here.
            # The stdio transport resolves the waiting future before reaching this
            # point; a transport that issues no requests has nothing to resolve and
            # simply accepts it, which is what the spec's `202` means. Anything else
            # carrying no method is malformed.
            if "id" in message and ("result" in message or "error" in message):
                return None
            return _error(msg_id, _JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")

        # Unlike base JSON-RPC, MCP forbids a null request id: a request either
        # carries a string or integer id, or omits the key entirely to be a
        # notification. A present-but-null id is neither, so answering it would
        # invent a correlation the client cannot use.
        if "id" in message and msg_id is None:
            return _error(None, _JSONRPC_INVALID_REQUEST, "Request id must not be null")

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
        is_modern = is_modern_version(requested_version)
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

        # Task augmentation is a property of `tools/call` alone. Enforced here
        # rather than in each handler: guarding them one at a time left most of
        # the surface answering a task-augmented request synchronously while the
        # caller waited for a handle to poll, which is the failure the check
        # exists to prevent. Notifications are exempt - a one-way message has no
        # response to carry the refusal, and nothing to poll for.
        if not is_notification and method != "tools/call" and "task" in params:
            return InvalidParamsError(
                f"{method} does not support task execution; call it without a 'task' field."
            ).to_error(msg_id)

        handler = self._methods.get(method)
        if handler is not None and is_modern and method in self._handshake_only:
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
        # Publish the era resolved above so every shaping function reads one
        # answer instead of re-deriving its own from `params`.
        era_token = _era_modern_var.set(is_modern)
        # A fresh slot per message, so `_meta` a handler attaches belongs to this
        # call's result and cannot reach the next one. Bound eagerly rather than
        # on first access: a sync tool handler runs in a copied context, so a
        # `set()` performed inside it would be discarded with the copy and the
        # handler's `_meta` would never be sent. An empty dict reads as no `_meta`
        # at the attach site below.
        result_meta_token = _result_meta_var.set({})
        release_tokens = True
        # Read while the slot is still bound: the `finally` below releases it, and
        # the result is not assembled until after that.
        attached_meta: dict[str, Any] | None = None
        try:
            result = await handler(params)
            attached_meta = _result_meta_var.get()
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
        except GeneratorExit:
            # Closed mid-await: an abandoned request being finalized, which the
            # collector may run in any context. These tokens belong to the context
            # that made the call, so resetting them here raises and would abandon
            # the rest of this block - including the registry release below.
            release_tokens = False
            raise
        finally:
            if release_tokens:
                _inflight_var.reset(token)
                _session_var.reset(session_token)
                _request_id_var.reset(request_id_token)
                _era_modern_var.reset(era_token)
                _result_meta_var.reset(result_meta_token)
                if log_level_token is not None:
                    _log_level_var.reset(log_level_token)
            # Context-free, so it is released on every path: an entry left behind
            # keeps the request cancellable forever and grows the registry.
            if inflight is not None:
                self._inflight.pop(inflight_key, None)

        if is_notification:
            return None
        if result is _DEFERRED_RESPONSE:
            # A long-lived request answered by its own closure, not here.
            return None
        # `resultType` is required on every modern result and must never reach a
        # legacy client - its revision has no such field. The shared guard is
        # tested once rather than repeated per arm, and the "already tagged" case
        # is stated instead of being an uncommented bare `pass`.
        if is_modern and isinstance(result, dict):
            if "resultType" in result:
                # A handler that tagged its own result keeps that tag.
                pass
            elif "task" in result:
                # A task handle is its own result type; the client polls rather
                # than reading a completed answer here.
                result = {"resultType": RESULT_TYPE_TASK, **result}
            else:
                result = {"resultType": RESULT_TYPE_COMPLETE, **result}
                if method in _CACHEABLE_METHODS:
                    self._add_cache_hints(method, result, session)
        if attached_meta and isinstance(result, dict):
            result = _attach_result_meta(result, attached_meta)
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
        # A server with no scopes, no filter and no search catalogue has nothing to
        # narrow, so its listing stays a plain synchronous build - no awaits, no
        # per-tool predicate - and costs exactly what it did before any of the
        # three existed.
        tools: Iterable[MCPTool] = (
            self.registry.tools.values()
            if self._tool_filter is None
            and self._tool_search is None
            and not self._any_scoped_tools
            else await self._visible_tools()
        )
        return self._listing("tools", tools, _tool_key, self._describe_tool, params)

    def _resource_listing(
        self, params: dict[str, Any], page_size: int | None | _ServerPageSize = _SERVER_PAGE_SIZE
    ) -> dict[str, Any]:
        """Build the `resources/list` result.

        One implementation, called by the JSON-RPC handler and by
        `MCPContext.list_resources`. The context method used to have its own,
        which applied scope narrowing but not the connection's hidden set - so a
        handler enumerating the catalogue contradicted the client's own listing.
        """
        return self._listing(
            "resources",
            self._readable(self.resources.statics(), self._any_scoped_resources),
            _resource_key,
            _describe_resource,
            params,
            page_size=page_size,
        )

    async def _handle_resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._resource_listing(params)

    async def _handle_resource_templates_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._listing(
            "resourceTemplates",
            self._readable(self.resources.templates(), self._any_scoped_resources),
            _resource_key,
            _describe_resource_template,
            params,
        )

    def _prompt_listing(
        self, params: dict[str, Any], page_size: int | None | _ServerPageSize = _SERVER_PAGE_SIZE
    ) -> dict[str, Any]:
        """Build the `prompts/list` result. See `_resource_listing`."""
        return self._listing(
            "prompts",
            self._readable(self.prompts.prompts.values(), self._any_scoped_prompts),
            _prompt_key,
            _describe_prompt,
            params,
            page_size=page_size,
        )

    async def _handle_prompts_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._prompt_listing(params)

    @staticmethod
    def _readable(items: Iterable[_ScopedT], any_scoped: bool) -> Iterable[_ScopedT]:
        """Drop the resources / prompts whose scopes the calling principal lacks.

        The same check `resources/read` and `prompts/get` perform, so a primitive
        is never listed to a caller that would be refused it. `any_scoped` is
        decided once at construction: a server declaring no scopes hands the
        registry's own view straight back, walking nothing.
        """
        if not any_scoped:
            return items
        return [item for item in items if not _principal_lacks_scopes(item.tool.required_scopes)]

    def _listing(
        self,
        key: str,
        items: Iterable[Any],
        key_of: Callable[[Any], str],
        describe: Callable[[Any], dict[str, Any]],
        params: dict[str, Any],
        page_size: int | None | _ServerPageSize = _SERVER_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Shape one page of a catalogue into its list result.

        Paging happens before shaping, so a page costs the entries it emits
        rather than the whole catalogue. `nextCursor` is present only while more
        remain, which is what tells a client to ask again.

        `page_size` overrides the server's own; `None` returns the whole
        catalogue unpaged, which is what the `MCPContext` listings want - they
        answer a handler, not a client that can ask again for the next page.
        """
        # A connection that hid something sees the catalogue without it. Applied
        # before paging, so a hidden entry never occupies a slot on a page.
        session = _session_var.get()
        hidden = session.hidden if session is not None else None
        if hidden:
            items = [item for item in items if key_of(item) not in hidden]
        if isinstance(page_size, _ServerPageSize):
            size: int | None = self._page_size
        else:
            size = page_size
        page, cursor = paginate(items, key_of, params.get("cursor"), size)
        result: dict[str, Any] = {key: [describe(item) for item in page]}
        if cursor is not None:
            result["nextCursor"] = cursor
        return result

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
        capabilities = self._advertised_capabilities(_era_modern_var.get())
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

    def _advertised_capabilities(self, modern: bool = False) -> dict[str, Any]:
        """Collect every held capability's advertisement for the caller's revision.

        A `None` advertisement is dropped so a client never probes a primitive
        the app does not expose. The revision is passed to the capabilities whose
        `advertise` accepts it, so one whose methods a revision retired can
        withhold or narrow its entry instead of promising what the dispatcher
        would then answer with method-not-found. Which capabilities accept it is
        resolved once at construction, not per handshake.
        """
        capabilities: dict[str, Any] = {}
        for capability in self._capabilities:
            entry = (
                capability.advertise(modern=modern)
                if capability in self._era_aware_capabilities
                else capability.advertise()
            )
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
        icons = render_icons(self.server_icons)
        if icons is not None:
            info["icons"] = icons
        if self.server_website_url:
            info["websiteUrl"] = self.server_website_url
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
        capabilities = self._advertised_capabilities(_era_modern_var.get())
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

    def _add_cache_hints(
        self, method: str, result: dict[str, Any], session: MCPSession | None
    ) -> None:
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
            _CACHE_SCOPE_PRIVATE if self._varies_by_caller(method, session) else _CACHE_SCOPE_PUBLIC
        )

    def connection_is_stateful(self) -> bool:
        """Whether the connection being answered persists beyond this request.

        A stateful connection has an outbound channel and can carry per-connection
        state; a stateless request has neither, and nothing it is told survives
        the response.
        """
        session = self.current_session()
        return session is not None and session.persistent

    def connection_can_stream(self, session: MCPSession) -> bool:
        """Whether `session` holds an open outbound stream to push messages down.

        Weaker than `connection_is_stateful` and deliberately so: a stream that
        lives only for the request that opened it still delivers, which is the
        whole shape of a modern `subscriptions/listen`.
        """
        return self._connections is not None and self._connections.holds(session)

    def _varies_by_caller(self, method: str, session: MCPSession | None = None) -> bool:
        """Whether this method's result can differ between two authorized callers.

        `session` is the connection being answered, passed in rather than read
        from the dispatch context: the hints are stamped after that context has
        been released.
        """
        if method == "resources/read":
            # Its route runs the full request lifecycle under the caller's
            # principal, so the body it returns is caller-dependent by construction.
            return True
        if method == "server/discover":
            # Its capability block is built for the asking connection: it reflects
            # what that connection can be told (a stateful one is offered
            # per-connection features a stateless one is not) and the protocol
            # revision the caller stated, so two callers get different answers.
            # `public` would let a shared gateway serve one of those answers to
            # the other, and the result also carries `instructions` - server
            # prose a client feeds to its model, which must not be cross-served.
            return True
        if method not in _LIST_METHODS:
            return False
        # A handler on a stateful connection may narrow that connection's view
        # with `MCPContext.hide` at any point, so two connections to this server
        # can be shown different lists. Which of them hid something is not the
        # question a cache key can ask: an answer produced for one connection
        # must not be replayed to another, whether or not either has hidden
        # anything yet.
        if session is not None and session.persistent:
            return True
        if method == "tools/list":
            return self._tool_filter is not None or self._any_scoped_tools
        if method == "prompts/list":
            return self._any_scoped_prompts
        return self._any_scoped_resources

    def _unnarrowed_tools(self) -> dict[str, MCPTool] | None:
        """The registry's own name -> tool map when nothing can narrow it here.

        Three things can narrow what a caller is shown: a declared scope, a
        configured `tool_filter`, and what this connection hid. When none of them
        is in play the candidate set *is* the registry, which is fixed once the
        server is built - so rebuilding it per call buys nothing. `None` when
        anything could narrow it, and the full walk has to run.

        The first two are decided once at construction. The third is a set on the
        session, so it costs one lookup rather than a walk.
        """
        if self._tool_filter is not None or self._any_scoped_tools:
            return None
        session = _session_var.get()
        if session is not None and session.hidden:
            return None
        return self.registry.tools

    async def _visible_tools(self) -> list[MCPTool]:
        """The tools `tools/list` reports to the calling principal.

        The narrowed catalogue, unless the server publishes its catalogue through
        search - in which case the listing is the three search tools and every
        other tool is found through them.
        """
        search = self._tool_search
        if search is None:
            return await self._candidate_tools()
        # The listing is the search tools and nothing else, so narrowing the whole
        # catalogue only to discard it is work with no reader. Only when something
        # could hide one of the three does the full pass have to run.
        if self._unnarrowed_tools() is not None:
            return list(search.tools)
        return [tool for tool in await self._candidate_tools() if tool.name in search.names]

    async def _candidate_tools(self) -> list[MCPTool]:
        """The tools the calling principal may see, in registration order.

        Visibility is scoped by the *same* check `tools/call` performs, so a tool is
        never listed for a caller that cannot invoke it. What this connection hid
        narrows it further, and so does a configured filter; either can hide a tool,
        neither can reveal one the scope check rejected.

        Narrowing is not enforcement, and this is the half worth being exact about:
        a tool hidden by `MCPContext.hide` or by a `mount_mcp(tool_filter=...)`
        policy is still callable, because what a caller may invoke is decided by
        its declared scopes alone. `MCPContext.hide` documents the same rule.
        Anything that must be refused needs `required_scopes`; a filter that
        returns `False` only removes the entry from a listing.

        Every reader of "what may this caller see" goes through here - the listing,
        and the search tools that stand in for it - so a tool hidden from one is
        hidden from the other.

        The spec permits exactly this axis: the tool set "MAY vary by the
        authorization presented on the request [...] since credentials are
        per-request input, not connection state". It is evaluated per request and
        never memoized - the framework cannot know when a principal's grants change,
        and `tools/list` is a session-start call rather than a hot path.
        """
        unnarrowed = self._unnarrowed_tools()
        if unnarrowed is not None:
            return list(unnarrowed.values())
        principal = current_principal()
        session = _session_var.get()
        hidden = session.hidden if session is not None else None
        # The principal is read once rather than once per tool: it cannot change
        # inside this pass. Mirrors `_principal_lacks_scopes`, which is the same
        # check for a caller that is not already in hand.
        if principal is None:
            scoped = [
                tool
                for tool in self.registry.tools.values()
                if not tool.required_scopes and not (hidden and tool.name in hidden)
            ]
        else:
            holds = principal.has_scopes
            scoped = [
                tool
                for tool in self.registry.tools.values()
                if (not tool.required_scopes or holds(tool.required_scopes))
                and not (hidden and tool.name in hidden)
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
        """Return this tool's `tools/list` entry for the era being answered.

        The entry is memoized on the tool because it is a pure function of
        registration data: nothing it reads can change once the registry is
        built. The modern revision removed `execution` from `Tool` - task
        support is negotiated through the extension capability instead, so an
        entry carrying the field does not validate against the schema that
        client negotiated - and that second shape is memoized alongside the
        first. Only a task-capable tool differs, so a listing costs no more than
        before.

        The era is read here rather than chosen by the caller: every site that
        advertises a tool definition - `tools/list`, the `tool_search`
        catalogue, `MCPContext` - reaches this one function, so none of them can
        advertise the wrong shape by forgetting to ask.

        Callers treat the returned mapping as read-only - the dispatcher stamps
        `ttlMs` / `cacheScope` onto the enclosing result, never onto an entry.
        """
        entry = tool.listing_entry
        if entry is None:
            entry = tool.listing_entry = _build_tool_listing_entry(tool)
        if not _era_modern_var.get():
            return entry
        modern = tool.listing_entry_modern
        if modern is None:
            modern = tool.listing_entry_modern = (
                {key: value for key, value in entry.items() if key != "execution"}
                if "execution" in entry
                else entry
            )
        return modern

    async def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise InvalidParamsError("tools/call requires a string 'name'")
        version = _requested_version(params)
        tool = self.registry.resolve(name, version)
        if tool is None:
            if version is not None:
                raise InvalidParamsError(f"Unknown tool: {name} (version {version})")
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

        return await self._produce_tool_result(
            tool, arguments, started, _progress_token(params), params.get("_meta")
        )

    async def _produce_tool_result(
        self,
        tool: MCPTool,
        arguments: dict[str, Any],
        started: float,
        progress_token: str | int | None,
        request_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke a tool and shape its return into the `tools/call` result object.

        Shared by the synchronous `tools/call` and the background task runner so
        a tool invoked either way runs the same handler dispatch and produces the
        same result shape (one handler, two doors). Authorization is checked by
        the caller; instrumentation and in-band error shaping happen here so both
        callers report a call's real outcome identically.
        """
        try:
            result = await self._run_invoke(tool, arguments, progress_token, request_meta)
        except _InBandError as exc:
            # Surfaced verbatim, unlike a handler exception: an invalid argument
            # names the offending field and what was expected, and an unavailable
            # client capability names the capability - both derived from the
            # tool's own declaration and the connection's own handshake, so there
            # is nothing to redact. Redacting would leave the model with
            # "internal error" and nothing to correct, which is the opposite of
            # what an execution error is for.
            await self._instrument(tool, started, _in_band_status_for(exc))
            return _text_result(str(exc), is_error=True)
        except asyncio.TimeoutError:
            await self._instrument(tool, started, status.HTTP_504_GATEWAY_TIMEOUT)
            return _text_result(
                f"tool call exceeded the {self._call_timeout}s timeout", is_error=True
            )
        except MCPError as exc:
            # An error the author raised deliberately, naming a JSON-RPC code and
            # writing its message: a tool reading a scoped resource hits the same
            # forbidden check a direct `resources/read` would, and a handler that
            # cannot answer until an elicitation completes has to say so with the
            # code the spec assigns. Flattening either into an in-band "internal
            # error" would discard both the code and the message the author wrote,
            # leaving the model nothing to act on. The `_InBandError` subtree is
            # caught above: those are execution failures, which belong in-band.
            await self._instrument(tool, started, _http_status_for(exc))
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
        if tool.passthrough_result and isinstance(shaped, dict) and "content" in shaped:
            # A forwarded call already answered in the result shape; relaying it
            # keeps the upstream's `isError` and `structuredContent` intact.
            return shaped
        # A pure tool's `output_schema` is advertised from its declared return
        # type, but nothing on the pure path guarantees the handler actually
        # returned that type. Validate / coerce the raw return through the
        # declared model so the emitted `structuredContent` conforms to the
        # advertised schema (the MCP MUST). A value that cannot be coerced to the
        # schema's object shape is an in-band error, not a non-conforming result.
        if tool.output_model is not None:
            try:
                shaped = shape_through_model(shaped, tool.output_model)
            except Exception:
                return _text_result(
                    "tool result does not conform to the declared output schema", is_error=True
                )
        return self._success_result(tool, shaped)

    # ── Resources ─────────────────────────────────────────

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
            body = _stringify(_response_body_value(response), self._result_dumps)
            if response.status_code == status.HTTP_404_NOT_FOUND:
                raise ResourceNotFoundError(body)
            if response.status_code in (
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN,
            ):
                raise _ForbiddenError(body)
            raise InternalError(body)
        return {"contents": [_resource_contents(uri, response, _declared_mime_type(resource))]}

    # ── Prompts ───────────────────────────────────────────

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
        out: dict[str, Any] = {"messages": _normalize_prompt_messages(result, self._result_dumps)}
        if prompt.description:
            out["description"] = prompt.description
        return out

    # ── Logging ───────────────────────────────────────────

    def _set_log_level(self, params: dict[str, Any]) -> dict[str, Any]:
        """Set the minimum level for ``notifications/message`` (logging/setLevel).

        Recorded on the session, because the spec scopes the level to the
        connection. The ContextVar is set too so the request that carried the
        call sees its own change immediately; the session is what carries it to
        the next request.
        """
        level = params.get("level")
        if not isinstance(level, str) or level not in _LOG_RANKS:
            raise InvalidParamsError("logging/setLevel requires a valid RFC 5424 'level'")
        # The dispatcher exposes the connection's session here, which is where
        # the level belongs: the ContextVar alone died with the request that set
        # it on every transport but the serial stdio loop.
        session = _session_var.get()
        if session is not None:
            session.log_level = level
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
