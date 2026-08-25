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

import secrets
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from veloce._internal import _current_request_var
from veloce.contrib.mcp.errors import MCPCapabilityError, MCPError
from veloce.contrib.mcp.sampling import (
    _NO_MORE_TOOLS,
    SampledToolCall,
    SamplingRun,
    content_blocks,
)

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.session import MCPSession
from veloce.principal import current_principal

# What a sampling request may ask the client to attach to the prompt. The client
# MAY ignore the request, so this is a hint; a value outside the set is a typo the
# client would silently drop, so it is refused here.
_SAMPLING_CONTEXT_MODES = frozenset({"none", "thisServer", "allServers"})

# Told to a connection whose visibility changed, one per kind of primitive whose
# listing actually changed. Sending all three would announce a change to lists
# that did not change - and, on a server exposing no prompts or resources, would
# use capabilities `initialize` never negotiated, which the lifecycle rules
# forbid. Ordered so a client sees them in a stable order.
_LIST_CHANGED_BY_KIND = (
    ("tools", "notifications/tools/list_changed"),
    ("prompts", "notifications/prompts/list_changed"),
    ("resources", "notifications/resources/list_changed"),
)

# Bytes of entropy in a minted `elicitationId`. The id names one interaction so a
# later `notifications/elicitation/complete` can be matched to it; it travels to
# the client, so it is unguessable rather than sequential.
_ELICITATION_ID_ENTROPY_BYTES = 16

# Whether the current call is running as a background task rather than inline.
# Set by the task runner; read by `MCPContext.is_background_task` and by the stdio
# transport. It lives here rather than in `_helpers` because `_helpers` imports this
# module, so the dependency has to run in this direction.
_in_task_var: ContextVar[bool] = ContextVar("_mcp_in_task", default=False)

# The id of the task this call is running as, and the id of the `tools/call`
# that created it. Both are set by the task runner alongside `_in_task_var`, and
# both stay `None` for an inline call. They live here for the same reason as
# `_in_task_var`: `_helpers` imports this module, so the dependency has to run
# in this direction.
_task_id_var: ContextVar[str | None] = ContextVar("_mcp_task_id", default=None)
_origin_request_id_var: ContextVar[Any] = ContextVar("_mcp_origin_request_id", default=None)

# The name of the transport serving this call, set once by each transport when
# it starts serving. `None` for a bare off-transport construction.
_transport_var: ContextVar[str | None] = ContextVar("_mcp_transport", default=None)

# `_meta` the handler asked to send back on this call's result. It lives here for
# the same reason as `_in_task_var`: `_helpers` imports this module, so the
# dependency has to run in this direction. `None` means the handler asked for
# nothing, which is the common case and costs one lookup.
_result_meta_var: ContextVar[dict[str, Any] | None] = ContextVar("_mcp_result_meta", default=None)

# The session of the connection being served, when the transport keeps one. It
# lives here for the same reason as the vars above: `_helpers` imports this
# module, so the dependency has to run in this direction. `None` off a stateful
# transport, where there is no connection to carry state.
_session_var: ContextVar[MCPSession | None] = ContextVar("_mcp_session", default=None)

# Suppresses every `notifications/message`, whatever its level. The modern revision
# sets the log level per request and requires a server to send no log notifications
# for a request that named none, which no RFC 5424 level can express.
LOG_LEVEL_OFF = "off"

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


def _error_content(message: str) -> dict[str, Any]:
    """Shape a failed sampled tool call the way a tool's own error result is shaped."""
    from veloce.contrib.mcp._helpers import _text_result  # breaks _helpers->context cycle

    return _text_result(message, is_error=True)


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
        session: Any = None,
        server: Any = None,
        request_meta: dict[str, Any] | None = None,
        request_id: Any = None,
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
        # The `_meta` the client sent with this request, verbatim. The protocol
        # reserves it for what the spec does not define, so what it holds is
        # between the client and whatever reads it here.
        self._request_meta = request_meta
        # The JSON-RPC id of the call being served, passed in rather than read
        # from a var here: the var lives in `_helpers`, which imports this
        # module, so the dependency has to run the other way.
        self._request_id = request_id
        self._log_level = log_level
        # Server->client request issuer (wired only by a bidirectional transport;
        # `None` for a one-way or off-transport construction) and the capabilities
        # the client advertised in `initialize` - together they make `sample` /
        # `elicit` / `roots` live, gated on the matching client capability.
        self._requester = requester
        # The dispatching connection and the serving `MCPServer`. Both are held as
        # references rather than unpacked into fields: every value they expose is a
        # property computed on access, so a call that never asks pays nothing beyond
        # the two assignments. `None` off a stateful transport / bare construction.
        self._session = session
        self._server = server
        # Resolved once here rather than on every capability gate. A direct
        # attribute read rather than `getattr` with a default: `session` is either a
        # session or `None`, and this runs on every tool call.
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
        `request.state`, so a dependency and the handler holding this context
        read and write one store rather than two that could disagree. Scoped to
        the call: a later `tools/call` starts clean.

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

        Unique across processes, so it stays a safe key for per-client state
        under a multi-worker server. It identifies a *connection*, not a client:
        a reconnecting client gets a new one, and under HTTP without a shared
        `session_backend` a client that lands on another worker does too.
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
        progress token, a trace id, an extension's own block - so a handler that
        needs to read or relay what the client attached finds it here.
        """
        return self._request_meta or {}

    @property
    def client_id(self) -> str | None:
        """The authenticated caller's id, or None when the call is unauthenticated.

        The subject of the principal the transport established - for a
        client-credentials token that is the registered MCP client. `client_info`
        is what the client *said* it was at `initialize`; this is what it proved.
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

        `None` off a transport. A handler that needs to know whether a
        server-initiated request can reach the client should ask
        `client_supports(...)` instead; this is for logging and diagnostics.
        """
        return _transport_var.get()

    @property
    def lifespan_context(self) -> Any:
        """The application state established at startup.

        The same `app.state` an HTTP handler reaches, so a connection pool or a
        client opened in a lifespan hook is reached the same way through either
        door. `None` off a server.
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
        """Send a debug-level log message to the client."""
        await self.log("debug", message, logger)

    async def info(self, message: Any, logger: str | None = None) -> None:
        """Send an info-level log message to the client."""
        await self.log("info", message, logger)

    async def warning(self, message: Any, logger: str | None = None) -> None:
        """Send a warning-level log message to the client."""
        await self.log("warning", message, logger)

    async def error(self, message: Any, logger: str | None = None) -> None:
        """Send an error-level log message to the client."""
        await self.log("error", message, logger)

    async def log(self, level: str, message: Any, logger: str | None = None) -> None:
        """Send a log message to the MCP client (notifications/message).

        Dropped when no notification channel is wired, or when `level` is below the
        client's `logging/setLevel` minimum.
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

    def _require_server(self, what: str) -> Any:
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
        declared scopes are enforced against the calling principal exactly as they
        would be for a direct client read - a tool cannot reach a resource its
        caller could not have read itself.
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
        """
        server = self._require_server("get_prompt")
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        rendered: dict[str, Any] = await server._prompts_get(params)
        return rendered

    def list_resources(self) -> list[dict[str, Any]]:
        """List this server's registered resources, as `resources/list` reports them.

        Routed through the one listing builder rather than a private twin, so a
        primitive this connection hid with `hide()` is absent here too - the
        twin applied scope narrowing and not the hidden set, so a handler
        enumerating the catalogue contradicted what the client's own listing
        showed. Unpaged: a handler cannot ask again for the next page.
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
        """
        if self._notifier is None:
            return
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._notifier(message)

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
        include_context: str | None = None,
    ) -> dict[str, Any]:
        """Ask the client's LLM to sample a completion (sampling/createMessage).

        Returns the client's result (its chosen model, role, and content). Requires
        a bidirectional transport and a client that advertised the ``sampling``
        capability; `tools` / `tool_choice` additionally require ``sampling.tools``.

        `include_context` asks the client to attach context from MCP servers to the
        prompt - ``"none"``, ``"thisServer"``, or ``"allServers"``. The client MAY
        ignore the request, so it is a hint rather than a guarantee.
        """
        if include_context is not None and include_context not in _SAMPLING_CONTEXT_MODES:
            raise ValueError(
                f"include_context must be one of {sorted(_SAMPLING_CONTEXT_MODES)}, "
                f"got {include_context!r}"
            )
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
        if include_context is not None:
            params["includeContext"] = include_context
        return await self._request("sampling/createMessage", "sampling", params)

    async def sample_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[str],
        max_tokens: int,
        max_tool_rounds: int = 5,
        model_preferences: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        stop_sequences: list[str] | None = None,
        tool_choice: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SamplingRun:
        """Sample with tools, executing the ones the model asks for, until it answers.

        `tools` names tools of this server the model may drive. Each request the
        model makes runs through the same path `tools/call` serves - declared
        scopes, call hooks, timeout and error shaping included - and its result
        is fed back as the next message. Returns a `SamplingRun` carrying the
        answer, the transcript, and every tool call made.

        `max_tool_rounds` caps how many times tools are executed. On the round
        after the cap the model is asked to answer without tools, so a run ends
        with an answer rather than an unanswered request; a client that ignores
        that instruction ends the run where it stands.

        Requires a client that advertised ``sampling.tools``.
        """
        server = self._require_server("sample_with_tools")
        declared = self._declare_tools(server, tools)
        allowed = frozenset(tools)

        transcript = list(messages)
        calls: list[SampledToolCall] = []
        for round_index in range(max_tool_rounds + 1):
            final_round = round_index == max_tool_rounds
            # A copy per round: the request holds this list until it is written,
            # and the loop appends to the transcript as soon as it returns.
            result = await self.sample(
                list(transcript),
                max_tokens=max_tokens,
                model_preferences=model_preferences,
                system_prompt=system_prompt,
                temperature=temperature,
                stop_sequences=stop_sequences,
                tools=declared,
                tool_choice=_NO_MORE_TOOLS if final_round else tool_choice,
                metadata=metadata,
            )
            blocks = content_blocks(result)
            requested = [block for block in blocks if block.get("type") == "tool_use"]
            if not requested or final_round:
                # The answer closes the transcript. Returning it without the final
                # turn would make `messages` unusable for the thing it is offered
                # for - extending into another run - by dropping the reply from
                # the history the next run is built on.
                transcript.append({"role": "assistant", "content": blocks})
                return SamplingRun(
                    content=tuple(blocks),
                    model=result.get("model"),
                    stop_reason=result.get("stopReason"),
                    messages=tuple(transcript),
                    tool_calls=tuple(calls),
                    rounds=round_index + 1,
                )
            transcript.append({"role": "assistant", "content": blocks})
            results = []
            for block in requested:
                call = await self._run_sampled_tool(server, block, allowed)
                calls.append(call)
                answer: dict[str, Any] = {
                    "type": "tool_result",
                    "toolUseId": call.id,
                    "content": call.result.get("content") or [],
                    "isError": call.is_error,
                }
                # A tool declaring an output schema answers with the value itself,
                # and the block has a field for it. Sending only the text would
                # hand the model a different shape than a direct `tools/call`
                # caller gets for the same tool.
                structured = call.result.get("structuredContent")
                if structured is not None:
                    answer["structuredContent"] = structured
                results.append(answer)
            transcript.append({"role": "user", "content": results})
        raise AssertionError("unreachable: the final round always returns")

    def _declare_tools(self, server: Any, names: list[str]) -> list[dict[str, Any]]:
        """Return the `tools/list` entries for the named tools of this server."""
        declared: list[dict[str, Any]] = []
        for name in names:
            tool = server.registry.get(name)
            if tool is None:
                raise ValueError(
                    f"sample_with_tools names {name!r}, which this server does not "
                    f"register; it has {sorted(server.registry.tools) or 'no tools'}"
                )
            declared.append(server._describe_tool(tool))
        return declared

    async def _run_sampled_tool(
        self, server: Any, block: dict[str, Any], allowed: frozenset[str]
    ) -> SampledToolCall:
        """Execute one tool the model asked for, as `tools/call` would.

        A failure comes back as an error result rather than propagating: the
        model asked for this call and can correct itself given the reason,
        whereas raising would end the handler on a bad argument it generated.
        """
        name = block.get("name")
        arguments = block.get("input")
        if not isinstance(arguments, dict):
            arguments = {}
        use_id = str(block.get("id") or "")
        if not isinstance(name, str) or name not in allowed:
            return SampledToolCall(
                name=str(name),
                id=use_id,
                arguments=arguments,
                result=_error_content(f"{name!r} is not one of the tools offered for this run"),
                is_error=True,
            )
        try:
            result = await server._tools_call({"name": name, "arguments": arguments})
        except MCPError as exc:
            return SampledToolCall(
                name=name,
                id=use_id,
                arguments=arguments,
                result=_error_content(str(exc)),
                is_error=True,
            )
        return SampledToolCall(
            name=name,
            id=use_id,
            arguments=arguments,
            result=result,
            is_error=bool(result.get("isError")),
        )

    async def elicit(
        self,
        message: str,
        *,
        requested_schema: dict[str, Any] | None = None,
        url: str | None = None,
        elicitation_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask the client to gather input from its user (elicitation/create).

        Form mode passes a `requested_schema` (the JSON Schema of the fields to
        collect); URL mode passes a `url` the client opens instead. Returns the
        client's response (its action and any collected content). Requires a
        bidirectional transport and a client that advertised ``elicitation``.

        URL mode also carries the `elicitationId` the spec requires, which names
        the interaction in a later `notifications/elicitation/complete`. One is
        minted per call; pass `elicitation_id` to use an identifier the URL flow
        already knows, so the completion can be correlated with whatever happens
        out of band.
        """
        if requested_schema is not None and url is not None:
            raise ValueError("elicit takes either requested_schema (form) or url, not both")
        params: dict[str, Any] = {"message": message}
        if url is not None:
            # A client advertising `elicitation` without `url` cannot open one, and
            # the spec forbids sending a mode the client did not declare. An empty
            # `elicitation: {}` means form-only, which this rejects correctly - so
            # only URL mode is gated, never form.
            self._require_sub_capability("elicitation", "url")
            params["mode"] = "url"
            params["url"] = url
            params["elicitationId"] = elicitation_id or secrets.token_urlsafe(
                _ELICITATION_ID_ENTROPY_BYTES
            )
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

    async def hide(self, *names: str) -> None:
        """Hide tools, prompts or resources from this connection's listings.

        Names a tool or prompt by name and a resource by URI. The change belongs
        to the connection that made the call - another client's listings are
        untouched - and this connection is told its lists changed so it fetches
        them again.

        Hiding is not enforcement. A hidden primitive is still callable, exactly
        as with a `mount_mcp(tool_filter=...)` policy: what a caller may invoke is
        decided by its declared scopes, so a hidden name cannot be mistaken for a
        permission boundary.
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
        # Only this connection is told: the change was made for it alone, and
        # only about the lists the changed names actually belong to.
        kinds = self._changed_kinds(changed)
        for kind, method in _LIST_CHANGED_BY_KIND:
            if kind in kinds:
                await self.send_notification(method, {})

    def _changed_kinds(self, names: set[str]) -> frozenset[str]:
        """Return which listings the given names appear in.

        A hidden name is a tool or prompt name, or a resource URI; which one it
        is decides who needs telling. Off a server - a bare context - nothing can
        be resolved, so every listing is treated as affected rather than
        silently telling no one.
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

    @property
    def result_meta(self) -> dict[str, Any]:
        """Scratch `_meta` sent back on this call's result.

        The protocol reserves `_meta` for metadata it does not define, so this is
        where a handler puts what its client agreed to read - a cost, a trace id,
        an extension's own block. Mutated in place::

            ctx.result_meta["io.example/trace"] = trace_id

        It belongs to one call: the next one starts empty. A handler that never
        touches it sends no `_meta` at all.
        """
        meta = _result_meta_var.get()
        if meta is None:
            # Only outside a served call - the server binds the slot before the
            # handler runs, so a write from a sync (offloaded) handler mutates a
            # dict both contexts share rather than one this copy would discard.
            meta = {}
            _result_meta_var.set(meta)
        return meta

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
