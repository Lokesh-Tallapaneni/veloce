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
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from veloce.contrib.mcp._calls import CallsMixin
from veloce.contrib.mcp._handshake import HandshakeMixin
from veloce.contrib.mcp._helpers import (
    _DEFERRED_RESPONSE,
    META_LOG_LEVEL,
    META_PROTOCOL_VERSION,
    _attach_result_meta,
    _era_modern_var,
    _InFlight,
    _inflight_var,
    _log_level_var,
    _notifier_var,
    _request_id_var,
    _requester_var,
)
from veloce.contrib.mcp._invocation import InvocationMixin
from veloce.contrib.mcp._listing import ListingMixin
from veloce.contrib.mcp._protocol import (
    _CACHEABLE_METHODS,
)
from veloce.contrib.mcp._protocol import (
    _CORE_HANDSHAKE_ONLY_METHODS as _CORE_HANDSHAKE_ONLY_METHODS,
)
from veloce.contrib.mcp._protocol import (
    _SERVED_VERSION_SET as _SERVED_VERSION_SET,
)
from veloce.contrib.mcp._protocol import (
    _SUPPORTED_PROTOCOL_VERSIONS as _SUPPORTED_PROTOCOL_VERSIONS,
)

# Names tests and sibling modules have always imported from here.
from veloce.contrib.mcp._protocol import (
    DEFAULT_CACHE_TTL_MS as DEFAULT_CACHE_TTL_MS,
)
from veloce.contrib.mcp._protocol import (
    LATEST_PROTOCOL_VERSION as LATEST_PROTOCOL_VERSION,
)
from veloce.contrib.mcp._protocol import (
    MODERN_PROTOCOL_VERSION as MODERN_PROTOCOL_VERSION,
)
from veloce.contrib.mcp._protocol import (
    PRIOR_PROTOCOL_VERSION as PRIOR_PROTOCOL_VERSION,
)
from veloce.contrib.mcp._protocol import (
    RESULT_TYPE_COMPLETE as RESULT_TYPE_COMPLETE,
)
from veloce.contrib.mcp._protocol import (
    RESULT_TYPE_TASK as RESULT_TYPE_TASK,
)
from veloce.contrib.mcp._protocol import (
    SERVED_PROTOCOL_VERSIONS as SERVED_PROTOCOL_VERSIONS,
)
from veloce.contrib.mcp._protocol import (
    MethodHandler as MethodHandler,
)
from veloce.contrib.mcp._protocol import (
    _build_tool_listing_entry as _build_tool_listing_entry,
)
from veloce.contrib.mcp._protocol import (
    _in_band_status_for as _in_band_status_for,
)
from veloce.contrib.mcp._protocol import (
    _requested_version as _requested_version,
)
from veloce.contrib.mcp._protocol import (
    is_modern_version as is_modern_version,
)
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
    InvalidParamsError,
    MCPError,
    UnsupportedProtocolVersionError,
    _error,
)
from veloce.contrib.mcp.icons import coerce_icons
from veloce.contrib.mcp.prompts import PromptRegistry, build_prompt_registry
from veloce.contrib.mcp.registry import (
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
from veloce.json_provider import resolve_dumps

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.session import MCPSession

_logger = logging.getLogger(__name__)


class MCPServer(CallsMixin, ListingMixin, HandshakeMixin, TasksMixin, InvocationMixin):
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
        *,
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

        Args:
            session: Session the connection belongs to.
            sink: Coroutine function delivering one JSON-RPC message to the client.

        Returns:
            An opaque token for `unregister_connection`, or `None` when subscriptions are
                disabled.

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

        Args:
            message: Decoded JSON-RPC message.
            session: Session of a stateful transport; `None` for a stateless one.

        Returns:
            The response object, or `None` for a notification.

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

        Args:
            session: Session the stream belongs to.
            subscription_id: Id of the stream's originating request.

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

    async def _handle_set_log_level(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._set_log_level(params)

    # ── Method handlers ───────────────────────────────────

    # ── Resources ─────────────────────────────────────────

    # ── Prompts ───────────────────────────────────────────

    # ── Logging ───────────────────────────────────────────
