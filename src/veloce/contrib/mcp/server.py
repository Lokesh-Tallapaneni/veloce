"""MCPServer — dispatch JSON-RPC 2.0 method calls against the tool registry.

Transport-agnostic: a transport hands it decoded JSON-RPC request objects,
forwards the responses, and supplies the outbound notification sink
(`set_notifier`). Every call runs the handler through the shared
`DependencyResolver` and the same `app.add_instrumentation` hook, so `Depends()`,
`yield` teardown and `Security` behave exactly as on the HTTP path.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar, cast

import veloce.status as status
from veloce._internal import _is_async_callable, offload
from veloce._model_backend import shape_through_model
from veloce.contrib.mcp._helpers import (
    _DEFERRED_RESPONSE,
    META_LOG_LEVEL,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
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
    ToolError,
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

MethodHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]

# The MCP lifecycle spec returns this from ``initialize`` for an unknown one.
LATEST_PROTOCOL_VERSION = "2025-11-25"

#: The revision before `LATEST_PROTOCOL_VERSION`, still served.
PRIOR_PROTOCOL_VERSION = "2025-06-18"

# Revisions compatible with this ``tools`` surface; ``2025-03-26`` is older.
_SUPPORTED_PROTOCOL_VERSIONS = frozenset({PRIOR_PROTOCOL_VERSION, LATEST_PROTOCOL_VERSION})

# The first "modern" revision: no handshake, no session, everything in `_meta`.
MODERN_PROTOCOL_VERSION = "2026-07-28"


def is_modern_version(version: str | None) -> bool:
    """Whether `version` names a revision served in the modern shape.

    The revisions are ISO dates, so ordering them as strings orders them by date
    and a revision later than the first modern one is modern too. The one
    definition both the transport and the core read, so a hop's two ends cannot
    act on different requests.
    """
    return version is not None and version >= MODERN_PROTOCOL_VERSION


# Newest first, echoed verbatim in `server/discover` and in a version error.
SERVED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    MODERN_PROTOCOL_VERSION,
    LATEST_PROTOCOL_VERSION,
    PRIOR_PROTOCOL_VERSION,
)


RESULT_TYPE_COMPLETE = "complete"
RESULT_TYPE_TASK = "task"
# `ping` owns no capability, so its retirement is declared here instead.
_CORE_HANDSHAKE_ONLY_METHODS = frozenset({"ping"})

# The methods the spec requires caching hints on, for a `complete` result.
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
# The registries never change shape after startup, so a generous default is safe.
DEFAULT_CACHE_TTL_MS = 300_000
# A result that can differ between callers must not be cached by a shared proxy.
_CACHE_SCOPE_PUBLIC = "public"
_CACHE_SCOPE_PRIVATE = "private"

_SERVED_VERSION_SET = frozenset(SERVED_PROTOCOL_VERSIONS)

# A prompt or a resource: both are gated by the scopes of the `MCPTool` they carry.
_ScopedT = TypeVar("_ScopedT", bound="MCPPrompt | MCPResource")


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
    """Shape one registered tool into its `tools/list` entry."""
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
    # The author's own annotations overlay the verb-derived ones.
    annotations = _tool_annotations(tool.route_methods, tool.title)
    if tool.annotations:
        annotations = {**(annotations or {}), **tool.annotations}
    if annotations:
        entry["annotations"] = annotations
    if tool.output_schema is not None:
        entry["outputSchema"] = tool.output_schema
    # The spec's default is `"forbidden"`, so a non-opting tool omits the field.
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


def _tool_entry_legacy(tool: MCPTool) -> dict[str, Any]:
    """Return the tool's listing entry, built once and memoized on the tool.

    A pure function of registration data: nothing it reads can change once the
    registry is built.
    """
    entry = tool.listing_entry
    if entry is None:
        entry = tool.listing_entry = _build_tool_listing_entry(tool)
    return entry


def _tool_entry_modern(tool: MCPTool) -> dict[str, Any]:
    """Return the tool's entry for the modern revision, memoized alongside it.

    The modern revision removed `execution` from `Tool`, so an entry carrying the
    field does not validate against the schema that client negotiated. Only a
    task-capable tool differs; every other tool shares the one object.
    """
    modern = tool.listing_entry_modern
    if modern is not None:
        return modern
    entry = _tool_entry_legacy(tool)
    modern = tool.listing_entry_modern = (
        {key: value for key, value in entry.items() if key != "execution"}
        if "execution" in entry
        else entry
    )
    return modern


def _tool_describer(modern: bool) -> Callable[[MCPTool], dict[str, Any]]:
    """Return the describer for an era.

    Chosen once per listing rather than once per tool: reading the era inside the
    describer cost a `ContextVar` lookup and an attribute load for every entry.
    """
    return _tool_entry_modern if modern else _tool_entry_legacy


def _in_band_status_for(error: _InBandError) -> int:
    """Map an in-band failure to the status instrumentation should record."""
    if isinstance(error, _InvalidArgumentsError):
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    if isinstance(error, MCPCapabilityError):
        return status.HTTP_424_FAILED_DEPENDENCY
    if isinstance(error, ToolError):
        # RFC 9110 Sec. 15.5.10: a declared failure is no server fault.
        return status.HTTP_409_CONFLICT
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
    narrows to `int | None` once the sentinel is ruled out, and so an explicit
    `page_size=None` - the whole catalogue, unpaged - stays distinguishable from
    "the caller said nothing".
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
        capabilities: Sequence[Capability] | None = None,
    ) -> None:
        self.app = app
        extra_capabilities = tuple(capabilities or ())
        # `None` leaves listing unfiltered, so an app not opting in pays nothing.
        self._tool_filter = tool_filter
        # The spec requires `>= 0`; zero means "treat every result as stale".
        self._cache_ttl_ms = max(0, cache_ttl_ms)
        # `None` lists in full: a client ignoring `nextCursor` would lose the tail.
        if page_size is not None and page_size < 1:
            raise ValueError(f"page_size must be a positive integer or None, got {page_size!r}")
        self._page_size = page_size
        self.registry = registry if registry is not None else build_registry(app)
        self.resources = resources if resources is not None else build_resource_registry(app)
        self.prompts = prompts if prompts is not None else build_prompt_registry(app)
        # Bound now the registries exist, so a bad completer target fails at build.
        attach_completers(app, self.prompts, self.resources)
        self._tool_search: ToolSearch | None = None
        # Once here: the registries are fixed, so nothing walks them per request.
        self._any_scoped_tools = any(tool.required_scopes for tool in self.registry.tools.values())
        self._any_scoped_prompts = any(
            prompt.tool.required_scopes for prompt in self.prompts.prompts.values()
        )
        self._any_scoped_resources = any(
            resource.tool.required_scopes for resource in self.resources.resources.values()
        )
        self.server_name = app.title
        self.server_version = app.version
        # `None` when nothing is configured, so an app with no dialect pays nothing.
        self._result_dumps = resolve_dumps(app)
        # The spec separates the identifier `name` from the display `title`.
        self.server_title = app.title
        self.server_icons = coerce_icons(app.mcp_icons)
        self.server_website_url = app.website_url or None
        self.server_instructions = app.description or app.summary or None
        # The stdio serve loop is serial, so one forever-awaiting handler wedges it.
        config = getattr(app, "config", None)
        self._call_timeout = config.get("MCP_CALL_TIMEOUT") if config is not None else None
        # The spec's "initialization MUST be first", enforced per session.
        self._enforce_lifecycle = bool(
            config.get("MCP_ENFORCE_LIFECYCLE") if config is not None else False
        )
        self._subscriptions_enabled = bool(
            config.get("MCP_RESOURCE_SUBSCRIPTIONS") if config is not None else False
        )
        self._connections = ConnectionRegistry() if self._subscriptions_enabled else None
        self._tasks = TaskRegistry()
        # A new spec area is a capability added here, not a dispatcher branch.
        built_in: list[Capability] = [
            ToolsCapability(self),
            ResourcesCapability(self),
            PromptsCapability(self),
            CompletionsCapability(self),
            TasksCapability(self),
            LoggingCapability(self),
        ]
        # Off, the methods are absent, matching the `subscribe: false` advertised.
        if self._subscriptions_enabled:
            built_in.append(SubscriptionsCapability(self))
        # Caller-supplied last, so a collision resolves to theirs, not ours.
        if extra_capabilities:
            built_in.extend(extra_capabilities)
        self._capabilities: tuple[Capability, ...] = tuple(built_in)
        # Resolved once here rather than inspected on every handshake.
        self._era_aware_capabilities: frozenset[Capability] = frozenset(
            capability
            for capability in self._capabilities
            if "modern" in inspect.signature(capability.advertise).parameters
        )
        # Built once at construction so per-request dispatch is one dict lookup.
        self._methods: dict[str, MethodHandler] = self._build_method_map()
        self._handshake_only: frozenset[str] = _CORE_HANDSHAKE_ONLY_METHODS.union(
            *(capability.handshake_only_methods for capability in self._capabilities)
        )
        # Keyed by connection too: two clients routinely reuse msg id `1`.
        self._inflight: dict[tuple[int | None, Any], _InFlight] = {}

        # Built last, so the scope scan above is redone over its three new tools.
        if tool_search:
            self._tool_search = ToolSearch(self)
            self._any_scoped_tools = any(
                tool.required_scopes for tool in self.registry.tools.values()
            )

    def _build_method_map(self) -> dict[str, MethodHandler]:
        """Map each supported JSON-RPC method to its async handler.

        The lifecycle methods (`initialize`, `ping`, the `initialized` ack) are
        core to every server; the spec-area methods are contributed by the held
        capabilities.
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

        The stdio transport calls this once in its serve task, while the Streamable
        HTTP transport sets the var per request so concurrent calls never cross
        notifications.
        """
        _notifier_var.set(notifier)

    @staticmethod
    def set_requester(
        requester: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        """Wire the current context's server->client request issuer.

        A bidirectional transport (the stdio loop) calls this once in its serve task
        so a tool's `MCPContext.sample` / `elicit` / `roots` reaches the client. A
        one-way transport never calls it, leaving those methods to raise.
        """
        _requester_var.set(requester)

    @staticmethod
    def current_request_id() -> Any:
        """Return the JSON-RPC id being dispatched, or None for a notification."""
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

        Any `subscriptions/listen` streams the connection held go with it: a stream
        left registered would keep a dead session reachable by fan-out.
        """
        if self._connections is not None and token is not None:
            self._connections.forget_streams(token)
            self._connections.remove(token)

    def evict_session(self, session: MCPSession) -> None:
        """Reclaim everything an evicted session owns: its connection and tasks.

        Beyond unregistering the subscription connection, this cancels and drops the
        session's tasks - a never-settling one included - so an abandoned session
        cannot pin a task for the process lifetime.
        """
        if self._connections is not None:
            self._connections.remove_session(session)
        for task in self._tasks.owned_by(session.connection_id):
            if not task.is_terminal() and task.runner is not None and not task.runner.done():
                task.runner.cancel()
            self._tasks.drop(task)

    async def notify_resource_updated(self, uri: str) -> None:
        """Tell subscribed clients a resource changed (`notifications/resources/updated`).

        Fans out to every connection subscribed to `uri`; a no-op when
        subscriptions are disabled or nothing subscribed to it.
        """
        if self._connections is not None:
            await self._connections.notify_updated(uri)

    async def notify_resources_list_changed(self) -> None:
        """Tell clients the resource list changed (`notifications/resources/list_changed`).

        Fans out to every open connection; a no-op when subscriptions are disabled.
        """
        if self._connections is not None:
            await self._connections.notify_list_changed()

    async def notify_tools_list_changed(self) -> None:
        """Tell listening clients the tool list changed.

        Reaches only the `subscriptions/listen` streams that asked for
        `toolsListChanged`; the spec forbids sending a type a client did not ask for.
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

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        """The capabilities this server was built with, in registration order.

        What a capability owns - its methods, which of them are handshake-only, and
        how it advertises itself per protocol revision - is readable from each entry.
        """
        return self._capabilities

    async def handle_message(
        self, message: dict[str, Any], session: MCPSession | None = None
    ) -> dict[str, Any] | None:
        """Dispatch one decoded JSON-RPC request; return the response object.

        Returns `None` for a notification (a request with no ``id``), which carries
        no response per JSON-RPC 2.0 Sec. 4.1.

        A stateful transport passes its `session` so the server records the client's
        advertised capabilities and enforces the lifecycle ordering; the stateless
        HTTP transport passes none, leaving its fast path unaffected.
        """
        # ── 1. Envelope: is this a well-formed JSON-RPC 2.0 request at all ──
        msg_id = message.get("id")
        method = message.get("method")

        if message.get("jsonrpc") != "2.0":
            return _error(msg_id, _JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")
        if not isinstance(method, str):
            # A reply carries no method, and needs no response of its own.
            if "id" in message and ("result" in message or "error" in message):
                return None
            return _error(msg_id, _JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")

        # Unlike base JSON-RPC, MCP forbids a null request id.
        if "id" in message and msg_id is None:
            return _error(None, _JSONRPC_INVALID_REQUEST, "Request id must not be null")

        params = message.get("params") or {}
        is_notification = "id" not in message

        log_level_token = None

        # ── 2. Era: which protocol revision is this client speaking ──
        # Modern clients state it per request; legacy ones negotiate once.
        meta = params.get("_meta") if isinstance(params, dict) else None
        raw_version = meta.get(META_PROTOCOL_VERSION) if isinstance(meta, dict) else None
        requested_version: str | None = raw_version if isinstance(raw_version, str) else None
        is_modern = is_modern_version(requested_version)
        if is_modern and session is not None:
            # A modern client never sends `initialize`, so `_meta` is the source.
            session.record_request_meta(meta)
        if is_modern:
            # Per request: the spec gives one naming no level no notifications.
            raw_level = meta.get(META_LOG_LEVEL) if isinstance(meta, dict) else None
            level = raw_level if isinstance(raw_level, str) and raw_level in _LOG_RANKS else None
            log_level_token = _log_level_var.set(level if level is not None else LOG_LEVEL_OFF)
        if requested_version is not None and requested_version not in _SERVED_VERSION_SET:
            # Recoverable: the client picks from `supported` and retries.
            if is_notification:
                return None
            return UnsupportedProtocolVersionError(
                requested_version, SERVED_PROTOCOL_VERSIONS
            ).to_error(msg_id)

        # ── 3. Lifecycle: is this request allowed yet on this connection ──
        # On a stateful connection the initialization exchange MUST be first.
        if session is not None:
            rejection = self._gate_session(session, method, params, msg_id, is_notification)
            if rejection is not None:
                return rejection

        # ── 4. Admission: task augmentation, and the method's own guards ──
        # Here, so a new method cannot answer a task-augmented request untracked.
        if not is_notification and method != "tools/call" and "task" in params:
            return InvalidParamsError(
                f"{method} does not support task execution; call it without a 'task' field."
            ).to_error(msg_id)

        handler = self._methods.get(method)
        if handler is not None and is_modern and method in self._handshake_only:
            # The tasks extension retired these; a modern client polls `tasks/get`.
            handler = None
        if handler is None:
            if is_notification:
                return None
            return _error(msg_id, _JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")

        # `initialize` is excluded: the spec forbids cancelling it.
        connection_key = session.connection_id if session is not None else None
        inflight_key = (connection_key, msg_id)
        inflight = self._track_inflight(inflight_key, method) if not is_notification else None
        token = _inflight_var.set(inflight)
        # `None` on the stateless path leaves `resources/subscribe` to reject.
        session_token = _session_var.set(session)
        request_id_token = _request_id_var.set(msg_id)
        # Published so every shaping function reads one answer, not its own.
        era_token = _era_modern_var.set(is_modern)
        # Eagerly: a sync handler's copied context would discard a later `set()`.
        result_meta_token = _result_meta_var.set({})
        release_tokens = True
        attached_meta: dict[str, Any] | None = None
        try:
            result = await handler(params)
            attached_meta = _result_meta_var.get()
        except MCPError as exc:
            return exc.to_error(msg_id)
        except asyncio.TimeoutError:
            # A tools/call surfaces its own timeout in-band before reaching here.
            return _error(msg_id, _JSONRPC_INTERNAL_ERROR, "request exceeded the MCP call timeout")
        except Exception as exc:  # pragma: no cover - defensive
            _logger.exception("MCP method %s raised", method)
            return _error(msg_id, _JSONRPC_INTERNAL_ERROR, self._error_text(exc, "internal error"))
        except GeneratorExit:
            # Resetting another context's tokens raises, abandoning the release.
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
            # An entry left behind keeps the request cancellable forever.
            if inflight is not None:
                self._inflight.pop(inflight_key, None)

        if is_notification:
            return None
        # ── 5. Result shaping: resultType, cache hints, and `_meta` ──
        if result is _DEFERRED_RESPONSE:
            # A long-lived request answered by its own closure, not here.
            return None
        # `resultType` is required on a modern result and absent from a legacy one.
        if is_modern and isinstance(result, dict):
            if "resultType" in result:
                # A handler that tagged its own result keeps that tag.
                pass
            elif "task" in result:
                # A task handle is its own result type; the client polls for it.
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

        Recording happens regardless of the ordering policy. Under
        `MCP_ENFORCE_LIFECYCLE` any request but `initialize` / `ping` preceding
        initialization is an invalid-request error; notifications always pass,
        since the spec orders requests and not one-way messages.
        """
        if method == "initialize":
            session.record_initialize(params)
            return None
        if method == "notifications/initialized":
            session.initialized = True
            return None
        # A stateless session is never initialized, so ordering would reject a POST.
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

        Skips `initialize`, which the spec forbids cancelling, and any request not
        running inside a task. `key` is `(connection_key, msg_id)`, so a cancel
        from one connection cannot reach another's colliding id.
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

        An unknown id is ignored - the spec expects clients to race a completing
        request. The lookup is scoped to the dispatching connection, so a client
        can cancel only its own in-flight request, never a peer's colliding id.
        """
        request_id = params.get("requestId")
        # A list/object would make the lookup key unhashable.
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            return None
        session = _session_var.get()
        # A `subscriptions/listen` is never in-flight, so cancelling ends its stream.
        if session is not None and request_id in session.listen_streams:
            await self.close_listen_stream(session, request_id)
            return None
        connection_key = session.connection_id if session is not None else None
        holder = self._inflight.get((connection_key, request_id))
        if holder is not None:
            holder.cancel()
        return None

    # ── Dispatch adapters ─────────────────────────────────
    # Thin wrappers giving every entry the dispatch map's uniform shape.

    async def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._initialize(params)

    async def _handle_initialized(self, params: dict[str, Any]) -> None:
        return None

    async def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        # The spec'd reply to a liveness probe is an empty result object.
        return {}

    async def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        # With no scopes, no filter and no search catalogue there is nothing to narrow.
        tools: Iterable[MCPTool] = (
            self.registry.tools.values()
            if self._tool_filter is None
            and self._tool_search is None
            and not self._any_scoped_tools
            else await self._visible_tools()
        )
        describe = _tool_describer(_era_modern_var.get())
        return self._listing("tools", tools, _tool_key, describe, params)

    def _resource_listing(
        self, params: dict[str, Any], page_size: int | None | _ServerPageSize = _SERVER_PAGE_SIZE
    ) -> dict[str, Any]:
        """Build the `resources/list` result.

        Called by the JSON-RPC handler and by `MCPContext.list_resources`, so a
        handler enumerating the catalogue cannot contradict the client's own
        listing, nor drop either narrowing: the caller's scopes and the
        connection's hidden set.
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

        The same check `resources/read` and `prompts/get` perform, so a primitive is
        never listed to a caller that would be refused it. `any_scoped` is decided
        once at construction: a server declaring no scopes walks nothing.
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

        Paging happens before shaping, so a page costs the entries it emits rather
        than the whole catalogue, and `nextCursor` is present only while more
        remain. `page_size` overrides the server's own; `None` returns the whole
        catalogue unpaged, which is what the `MCPContext` listings want.
        """
        # Applied before paging, so a hidden entry never occupies a slot on a page.
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
        # Echo a supported revision, else the server's latest for the client to judge.
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
        if self.server_instructions:
            result["instructions"] = self.server_instructions
        return result

    def _advertised_capabilities(self, modern: bool = False) -> dict[str, Any]:
        """Collect every held capability's advertisement for the caller's revision.

        A `None` advertisement is dropped so a client never probes a primitive the
        app does not expose. The revision reaches each capability whose `advertise`
        accepts it, so one whose methods a revision retired withholds its entry
        rather than promising a method-not-found.
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

        The modern revision requires this single probe in place of the `initialize`
        handshake. `supportedVersions` is ordered newest-first, so a client taking
        the head gets the newest revision both sides serve, and `serverInfo` travels
        in `_meta`, where that revision moved it.
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
        """List the protocol extensions this server implements, for `server/discover`.

        A capability contributes an entry only when the feature it names is
        actually available, so a server with no task-capable tool advertises no
        tasks extension and a client will never offer one.
        """
        advertised: dict[str, Any] = {}
        for capability in self._capabilities:
            entry = capability.extensions()
            if entry:
                advertised.update(entry)
        return advertised

    def _add_cache_hints(
        self, method: str, result: dict[str, Any], session: MCPSession | None
    ) -> None:
        """Attach `ttlMs` / `cacheScope` to a cacheable `complete` result.

        A result that can differ between callers is `private`, so a shared gateway
        cannot serve one caller's answer to another; everything else is `public`.
        The spec is explicit that `cacheScope` is a hint and never a substitute for
        the per-primitive access control that already runs on each of these paths.
        """
        result["ttlMs"] = self._cache_ttl_ms
        result["cacheScope"] = (
            _CACHE_SCOPE_PRIVATE if self._varies_by_caller(method, session) else _CACHE_SCOPE_PUBLIC
        )

    def connection_is_stateful(self) -> bool:
        """Whether the connection being answered persists beyond this request.

        A stateless request has no outbound channel and no per-connection state, so
        nothing it is told survives the response.
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

        `session` is passed in rather than read from the dispatch context: the
        hints are stamped after that context has been released.
        """
        if method == "resources/read":
            # Its route runs the full lifecycle under the caller's principal.
            return True
        if method == "server/discover":
            # Built per connection, so `public` would leak it across callers.
            return True
        if method not in _LIST_METHODS:
            return False
        # `MCPContext.hide` can narrow one connection's view at any point.
        if session is not None and session.persistent:
            return True
        if method == "tools/list":
            return self._tool_filter is not None or self._any_scoped_tools
        if method == "prompts/list":
            return self._any_scoped_prompts
        return self._any_scoped_resources

    def _unnarrowed_tools(self) -> dict[str, MCPTool] | None:
        """Return the registry's own name -> tool map when nothing narrows it here.

        A declared scope, a configured `tool_filter` and what this connection hid
        can each narrow the catalogue. With none in play the candidate set *is* the
        registry, fixed once the server is built; `None` when the full walk has to
        run instead.
        """
        if self._tool_filter is not None or self._any_scoped_tools:
            return None
        session = _session_var.get()
        if session is not None and session.hidden:
            return None
        return self.registry.tools

    async def _visible_tools(self) -> list[MCPTool]:
        """List the tools `tools/list` reports to the calling principal.

        The narrowed catalogue, unless the server publishes its catalogue through
        search - in which case the listing is the three search tools and every
        other tool is found through them.
        """
        search = self._tool_search
        if search is None:
            return await self._candidate_tools()
        # Narrowing the whole catalogue only to discard it has no reader.
        if self._unnarrowed_tools() is not None:
            return list(search.tools)
        return [tool for tool in await self._candidate_tools() if tool.name in search.names]

    async def _candidate_tools(self) -> list[MCPTool]:
        """List the tools the calling principal may see, in registration order.

        Visibility is scoped by the *same* check `tools/call` performs, so a tool
        is never listed for a caller that cannot invoke it. What this connection
        hid and a configured filter narrow it further; neither can reveal a tool
        the scope check rejected.

        Narrowing is not enforcement: a tool hidden by `MCPContext.hide` or a
        `mount_mcp(tool_filter=...)` policy is still callable, because what a
        caller may invoke is decided by its declared scopes alone. Anything that
        must be refused needs `required_scopes`.

        The spec permits exactly this axis - the tool set "MAY vary by the
        authorization presented on the request [...] since credentials are
        per-request input, not connection state" - so it is evaluated per request
        and never memoized.
        """
        unnarrowed = self._unnarrowed_tools()
        if unnarrowed is not None:
            return list(unnarrowed.values())
        principal = current_principal()
        session = _session_var.get()
        hidden = session.hidden if session is not None else None
        # Read once rather than once per tool: it cannot change inside this pass.
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
                # `_is_async_callable` picked the branch; the alias unions both.
                if await cast("Awaitable[bool]", tool_filter(tool, principal)):
                    visible.append(tool)
            return visible
        # One handoff: offloading each predicate makes microseconds milliseconds.
        return cast(
            "list[MCPTool]",
            await offload(_apply_sync_tool_filter, tool_filter, scoped, principal),
        )

    @staticmethod
    def _describe_tool(tool: MCPTool) -> dict[str, Any]:
        """Return this tool's `tools/list` entry for the era being answered.

        The era is read here rather than chosen by the caller, so no site that
        advertises a tool definition can advertise the wrong shape by forgetting
        to ask. Callers treat the returned mapping as read-only.
        """
        return _tool_describer(_era_modern_var.get())(tool)

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
        # A protocol-level forbidden error, uniform across all three primitives.
        if _principal_lacks_scopes(tool.required_scopes):
            await self._instrument(tool, started, status.HTTP_403_FORBIDDEN)
            raise AuthorizationError(tool.required_scopes)

        # Started detached; the client retrieves the result via `tasks/result`.
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

        Shared by the synchronous `tools/call` and the background task runner, so
        both run the same handler dispatch and report the same outcome.
        Authorization is checked by the caller; instrumentation and in-band error
        shaping happen here.
        """
        try:
            result = await self._run_invoke(tool, arguments, progress_token, request_meta)
        except _InBandError as exc:
            # Verbatim: every message here was written for the agent to read.
            await self._instrument(tool, started, _in_band_status_for(exc))
            return _text_result(str(exc), is_error=True)
        except asyncio.TimeoutError:
            await self._instrument(tool, started, status.HTTP_504_GATEWAY_TIMEOUT)
            return _text_result(
                f"tool call exceeded the {self._call_timeout}s timeout", is_error=True
            )
        except MCPError as exc:
            # The author named a code and wrote the message; in-band loses both.
            await self._instrument(tool, started, _http_status_for(exc))
            raise
        except Exception as exc:
            # A pure `@app.mcp_tool` has no exception handlers to route through.
            await self._instrument(tool, started, status.HTTP_500_INTERNAL_SERVER_ERROR)
            return _text_result(
                self._error_text(exc, "the tool raised an internal error"), is_error=True
            )

        # These carry the real status code, which instrumentation must report.
        if isinstance(result, (_ShortCircuit, _RouteResponse)):
            response = result.response
            try:
                await self._drain_stream(response)
            except _InBandError as exc:
                # A refusal the author declared is a 409 wherever it was raised.
                await self._instrument(tool, started, _in_band_status_for(exc))
                return _text_result(str(exc), is_error=True)
            await self._instrument(tool, started, response.status_code)
            # A short-circuit response never went through `response_model`.
            model_filtered = isinstance(result, _RouteResponse) and result.model_filtered
            return self._result_from_response(tool, response, model_filtered)

        try:
            # `_drain_stream` is a no-op for a non-streaming value.
            if isinstance(result, Response):
                await self._drain_stream(result)
                # An image/audio Response has no text form.
                binary = _binary_result(result)
                if binary is not None:
                    await self._instrument(tool, started, result.status_code)
                    return binary
            shaped = self._shape_result(tool, result)
        except _InBandError as exc:
            await self._instrument(tool, started, _in_band_status_for(exc))
            return _text_result(str(exc), is_error=True)
        await self._instrument(tool, started, status.HTTP_200_OK)
        if tool.passthrough_result and isinstance(shaped, dict) and "content" in shaped:
            # Relaying keeps the upstream's `isError` and `structuredContent`.
            return shaped
        # The pure path does not guarantee the declared type the schema promises.
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

        The response body becomes the resource contents: a JSON/`text/*` body as
        `text`, any other media type as a base64 `blob`. An unknown URI - or a route
        answering 404 - is a resource-not-found error; a handler 4xx/5xx surfaces as
        a JSON-RPC error, since a resource read has no in-band error channel.
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

        # A value the route cannot coerce already raises `InvalidParamsError`.
        result = await self._run_invoke(resource.tool, arguments, _progress_token(params))

        # Always route-backed, so the `Response` body is the resource contents.
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

        Its return - a string or a list of role/content messages - is normalised
        into the MCP messages ``prompts/get`` returns. An unknown name or a
        malformed argument is an invalid-params error.
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
        connection; the ContextVar is set too so the request that carried the call
        sees its own change immediately.
        """
        level = params.get("level")
        if not isinstance(level, str) or level not in _LOG_RANKS:
            raise InvalidParamsError("logging/setLevel requires a valid RFC 5424 'level'")
        session = _session_var.get()
        if session is not None:
            session.log_level = level
        _log_level_var.set(level)
        return {}

    def _error_text(self, exc: Exception, generic: str) -> str:
        """Error text for an in-band / internal error payload, gated by debug.

        A pure `@app.mcp_tool` bypasses the app's exception handlers, so a raw
        message could carry a secret - a DSN, a token, a path - and `app.debug` off
        surfaces a generic one. `handle_user_exception` already gates a
        route-backed tool's body on debug itself.
        """
        return str(exc) if getattr(self.app, "debug", False) else generic
