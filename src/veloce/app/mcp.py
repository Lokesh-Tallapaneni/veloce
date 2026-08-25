"""MCP registration — tools, prompts, completers, call hooks, and the mount.

Mixed into `Veloce`, alongside the other sibling mixins that keep one concern
each out of `core.py`. Everything here runs at registration or mount time, so
none of it is on a request path; the `veloce.contrib.mcp` imports are deferred
into the methods that need them so an app exposing no tools never loads the
subsystem at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.icons import Icon


class MCPMixin:
    """Register MCP tools, prompts, completers and hooks, and mount a server."""

    __slots__ = ()

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes the host application (`Veloce`) provides.
        _mcp_tools: Any
        _mcp_prompts: Any
        _mcp_completers: Any
        _mcp_prebuilt_tools: Any
        _mcp_before_call: Any
        _mcp_after_call: Any
        lifespan_context: Callable[..., Any]

    def _init_mcp_state(self) -> None:
        """The registries `mcp_tool` / `mcp_prompt` / `mcp_completer` write into."""
        # MCP-only tool registrations (contrib.mcp). Each entry is
        # `(handler, name, description, namespace, scopes, tags, icons,
        # task_support, declared, meta, version)`, recorded by
        # `@app.mcp_tool(...)` and consumed once at `mount_mcp` time when the
        # tool registry is assembled. The annotation below is the same shape.
        self._mcp_tools: list[
            tuple[
                Callable[..., Any],
                str | None,
                str | None,
                str | None,
                frozenset[str] | None,
                frozenset[str] | None,
                Any,
                bool,
                dict[str, Any] | None,
                dict[str, Any] | None,
                str | None,
            ]
        ] = []
        # MCP prompt registrations (contrib.mcp). Each entry is
        # `(handler, name, description, namespace, scopes, icons, meta)`, recorded by
        # `@app.mcp_prompt(...)` and consumed once at `mount_mcp` time when the
        # prompt registry is assembled.
        self._mcp_prompts: list[
            tuple[
                Callable[..., Any],
                str | None,
                str | None,
                str | None,
                frozenset[str] | None,
                Any,
                dict[str, Any] | None,
            ]
        ] = []
        # Hooks that run around every MCP call - tool, resource read or prompt
        # render - whichever way the primitive was registered. A route-backed tool
        # replays the HTTP request lifecycle and so already sees `before_request`;
        # a tool registered with `@app.mcp_tool` has no route, so these are the
        # only place a cross-cutting concern can sit for it. Empty lists cost one
        # falsy check per call.
        self._mcp_before_call: list[Callable[..., Any]] = []
        self._mcp_after_call: list[Callable[..., Any]] = []
        # MCP argument-completer registrations (contrib.mcp). Each entry is
        # `(kind, key, argument, completer)` where `kind` is "prompt" or
        # "resource", `key` is the prompt name or resource URI, recorded by
        # `@app.mcp_completer(...)` and bound onto its descriptor at `mount_mcp`
        # time so `completion/complete` can answer for that argument.
        self._mcp_completers: list[tuple[str, str, str, Callable[..., Any]]] = []

    def mcp_tool(
        self,
        description: str,
        *,
        name: str | None = None,
        namespace: str | None = None,
        scopes: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        icons: Sequence[Icon] | None = None,
        task_support: bool = False,
        annotations: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        version: str | None = None,
    ) -> Callable[..., Any]:
        """Register an MCP-only tool callable by an AI agent (contrib.mcp).

        The decorated coroutine (or sync function) becomes an MCP tool whose
        input JSON Schema is derived from its signature; `Depends()` params
        resolve through the same dependency machinery routes use, with an
        `MCPContext` standing in for the HTTP `Request`. `description` is the
        required LLM-facing text (separate from the docstring). `namespace`
        prefixes the tool name (`<namespace>_<name>`), mirroring how a
        blueprint namespaces an exposed route. `icons` is an optional list of
        `Icon` objects a client may render next to the tool. `task_support=True`
        lets a client run the tool as a background task (task-augmented
        `tools/call`, polled via `tasks/get` / `tasks/result`). `version` labels
        this registration: two tools sharing a name and declaring different
        versions are both registered, the higher one is listed, and a call
        naming no version reaches it.

        Usage::

            @app.mcp_tool(description="Add two integers")
            async def add(a: int, b: int) -> int:
                return a + b
        """
        from veloce.contrib.mcp.safety import require_mcp_description, validate_tool_annotations

        scope_set = frozenset(scopes) if scopes else None

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            require_mcp_description(name or func.__name__, description)
            # Validated here rather than at mount time, so a misspelled hint is
            # reported against the decorator that wrote it.
            declared = validate_tool_annotations(annotations)
            self._mcp_tools.append(
                (
                    func,
                    name,
                    description,
                    namespace,
                    scope_set,
                    frozenset(tags) if tags else None,
                    icons,
                    task_support,
                    declared,
                    meta,
                    version,
                )
            )
            return func

        return decorator

    def mcp_prompt(
        self,
        description: str,
        *,
        name: str | None = None,
        namespace: str | None = None,
        scopes: Sequence[str] | None = None,
        icons: Sequence[Icon] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Callable[..., Any]:
        """Register an MCP prompt template fetchable by an AI agent (contrib.mcp).

        The decorated callable's parameters become the prompt's arguments, and its
        return - a string, or a list of role/content messages - becomes the
        messages ``prompts/get`` returns. `Depends()` params resolve through the
        same dependency machinery routes use, with an `MCPContext` standing in for
        the HTTP `Request`. `description` is the required LLM-facing text;
        `namespace` prefixes the prompt name (`<namespace>_<name>`). `icons` is an
        optional list of `Icon` objects a client may render next to the prompt.

        Usage::

            @app.mcp_prompt(description="Summarise a topic in three bullets")
            async def summarise(topic: str) -> str:
                return f"Summarise {topic} in three bullet points."
        """
        from veloce.contrib.mcp.safety import require_mcp_description

        scope_set = frozenset(scopes) if scopes else None

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            require_mcp_description(name or func.__name__, description)
            self._mcp_prompts.append((func, name, description, namespace, scope_set, icons, meta))
            return func

        return decorator

    def add_mcp_tool(self, tool: Any) -> None:
        """Register an already-built `MCPTool` (contrib.mcp).

        The decorator builds a tool from a handler; this takes one that already
        exists - most often from `derive_tool`, which narrows a registered tool
        into the façade an agent should see::

            app.add_mcp_tool(derive_tool(internal, name="search", arguments={...}))
        """
        self._mcp_prebuilt_tools.append(tool)

    def before_mcp_call(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Register a hook that runs before every MCP call (contrib.mcp).

        Called with the primitive's name and the arguments it was given. Return
        `None` to let the call proceed, or any other value to answer with that
        instead of invoking the handler - the same short-circuit shape
        `before_request` has. Raising an `MCPError` reports the failure to the
        client, which is how an authorization check refuses a call.

        Unlike `before_request`, this reaches a tool registered with
        `@app.mcp_tool`, which has no route and so no request lifecycle::

            @app.before_mcp_call
            async def audit(name, arguments):
                log.info("mcp call", extra={"tool": name})
        """
        self._mcp_before_call.append(func)
        return func

    def after_mcp_call(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Register a hook that runs after every MCP call (contrib.mcp).

        Called with the primitive's name and the handler's return value, and
        returns the value to send on - so a hook may rewrite a result, or return
        it unchanged. Hooks run in registration order, each seeing what the last
        returned. It does not run when the call raised.
        """
        self._mcp_after_call.append(func)
        return func

    def mcp_completer(
        self,
        *,
        argument: str,
        prompt: str | None = None,
        resource: str | None = None,
    ) -> Callable[..., Any]:
        """Register an argument-value completer for an MCP prompt or resource (contrib.mcp).

        The decorated callable suggests values for one `argument` of a `prompt`
        (named) or a `resource` (by URI template) as the user types, answering the
        MCP ``completion/complete`` request. It is called with the partial value
        and a mapping of the sibling argument values already resolved, and returns
        a sequence of candidate strings (or a `CompletionResult` for explicit
        totals). Pass exactly one of `prompt` or `resource`. An argument with no
        registered completer answers with an empty completion.

        Usage::

            @app.mcp_completer(prompt="greet", argument="name")
            async def complete_name(value: str, context: dict[str, str]) -> list[str]:
                return [n for n in KNOWN_NAMES if n.startswith(value)]
        """
        if prompt is not None and resource is None:
            kind, key = "prompt", prompt
        elif resource is not None and prompt is None:
            kind, key = "resource", resource
        else:
            raise ValueError("mcp_completer requires exactly one of prompt= or resource=.")

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._mcp_completers.append((kind, key, argument, func))
            return func

        return decorator

    def mount_mcp(
        self,
        transport: str = "stdio",
        *,
        path: str = "/mcp",
        auth: Any = None,
        principal: Any = None,
        allowed_origins: Sequence[str] | None = None,
        exclude_middleware: Sequence[str] | None = None,
        sessions: bool = False,
        resumable: bool = False,
        tool_filter: Any = None,
        cache_ttl_ms: int | None = None,
        page_size: int | None = None,
        tool_search: bool = False,
        session_backend: Any = None,
        message_path: str = "/messages",
    ) -> Any:
        """Build the MCP server and serve the registered tools.

        Assembles the tool registry from `@app.mcp_tool` registrations plus every
        route flagged `expose_as_mcp_tool=True`, the resource registry from every
        read-only route flagged `expose_as_mcp_resource=True`, and the prompt
        registry from `@app.mcp_prompt` registrations, then serves them over the
        chosen transport.

        `transport="stdio"` (the default) serves JSON-RPC 2.0 on stdin/stdout for
        subprocess use and returns an awaitable serve coroutine that runs until
        stdin closes, inside the app's `lifespan_context()` - so every
        `on_startup` handler runs before the first tool is served. Schedule it
        explicitly (`asyncio.run(app.mount_mcp())`). A local subprocess is trusted,
        so authentication is from the environment: pass a `principal` (a
        `veloce.Principal`) to establish the identity / scopes the served tools run
        under.

        `transport="http"` mounts the Streamable HTTP transport as a `POST` route
        at `path` (default `/mcp`) on this app and returns `None`; serve the app
        with any ASGI server (or `app.run()`) as usual. Pass `auth` (a
        `veloce.contrib.mcp.MCPAuth`) to make the endpoint an OAuth 2.1 resource
        server - validating the bearer token on every request and serving the
        RFC 9728 metadata. `allowed_origins` enables `Origin` validation
        (DNS-rebinding defense); `exclude_middleware` names app middleware the
        transport routes opt out of (an app-wide auth middleware `auth` replaces).
        `sessions` opts into `Mcp-Session-Id` lifecycle: the server assigns a
        session id on `initialize`, requires it on later requests (400 missing,
        404 once terminated), and accepts a `DELETE` to terminate it.
        `resumable` opts into SSE resumability: each streamed event gets an id
        encoding its stream, and a `GET` carrying `Last-Event-ID` replays only that
        stream's missed events so a client can reconnect after a dropped connection.
        `tool_filter` narrows what `tools/list` reports per caller beyond the
        declared scopes: a callable `(tool, principal) -> bool` (sync or async) that
        hides tools an agent has no business seeing, so its context is not spent on
        tools it cannot invoke. Declared scopes are applied first, whether or not a
        filter is set - every list omits what this caller would be refused - so a
        filter can only hide further, never reveal; hiding a primitive does not
        change what happens if it is called anyway.
        `cache_ttl_ms` sets the freshness hint sent with cacheable results
        (`tools/list`, `prompts/list`, `resources/list`, `resources/read` and
        `server/discover`) on the modern protocol revision; `0` marks them
        immediately stale. A list that can differ between callers is additionally
        marked private so a shared proxy cannot serve one caller's answer to another.
        `transport="sse"` mounts the deprecated split-endpoint wire of MCP revision
        2024-11-05, for a client that speaks only that: a `GET` at `path`
        (defaulting to `/sse`) opens a stream that names `message_path` as the URL
        to POST to, each POST is acknowledged `202` and its JSON-RPC response
        arrives on the stream. Prefer `transport="http"` for anything new - one
        endpoint, and a dropped connection can be resumed.
        `session_backend` shares HTTP sessions between workers - any object with
        async `read` / `write` / `delete` methods over a `SessionRecord`. Without
        one a session lives in the worker that minted it, so a request reaching a
        different worker is answered 404 and the client starts a new session.
        `page_size` opts the list methods into cursor pagination: each answers with
        at most that many entries plus a `nextCursor` while more remain, so a large
        catalogue reaches the agent a page at a time instead of filling its context
        in one response. Left unset, every list is answered in full - a client may
        ignore `nextCursor`, so paginating uninvited would hide the rest of the
        catalogue from one that does.
        `tool_search` publishes three tools in place of the catalogue -
        `search_tools`, `describe_tools` and `run_tools` - so a server with a large
        catalogue spends the agent's context on the tools it turns out to need
        rather than on every tool it has. `run_tools` executes declared calls, not
        code: each step names a registered tool and its arguments, and a step's
        argument may reference an earlier step's result.
        Call this after the tool / resource / prompt routes are registered.
        """
        from veloce.contrib.mcp.server import DEFAULT_CACHE_TTL_MS, MCPServer

        # Omitted means the server's own default freshness hint.
        cache_ttl = DEFAULT_CACHE_TTL_MS if cache_ttl_ms is None else cache_ttl_ms

        if transport == "stdio":
            from veloce.contrib.mcp.transports.stdio import serve_stdio
            from veloce.principal import set_principal

            server = MCPServer(
                self,
                tool_filter=tool_filter,
                cache_ttl_ms=cache_ttl,
                page_size=page_size,
                tool_search=tool_search,
            )

            async def _serve() -> None:
                if principal is not None:
                    set_principal(principal)
                async with self.lifespan_context():
                    await serve_stdio(server)

            return _serve()

        if transport == "http":
            from veloce.contrib.mcp.transports.http import register_http_transport

            server = MCPServer(
                self,
                tool_filter=tool_filter,
                cache_ttl_ms=cache_ttl,
                page_size=page_size,
                tool_search=tool_search,
            )
            # A task-augmented call records the creating connection's identity and
            # the follow-up tasks/get|result|list|cancel must run under that same
            # connection. The stateless default mints a throwaway session (a fresh,
            # never-recycled connection id) per POST, so a task created by one POST
            # can never be retrieved by another. Require sessions=True so the
            # connection persists and the task remains reachable.
            if not sessions and any(tool.task_support for tool in server.registry.tools.values()):
                raise ValueError(
                    "MCP task support over the HTTP transport requires sessions=True; "
                    "pass mount_mcp(transport='http', sessions=True) so a task created "
                    "by one request can be retrieved by the follow-up tasks/* request."
                )

            register_http_transport(
                self,
                server,
                path=path,
                auth=auth,
                allowed_origins=(
                    frozenset(allowed_origins) if allowed_origins is not None else None
                ),
                exclude_middleware=exclude_middleware,
                sessions=sessions,
                resumable=resumable,
                session_backend=session_backend,
            )
            return None

        if transport == "sse":
            from veloce.contrib.mcp.transports.sse import register_sse_transport

            server = MCPServer(
                self,
                tool_filter=tool_filter,
                cache_ttl_ms=cache_ttl,
                page_size=page_size,
                tool_search=tool_search,
            )
            register_sse_transport(
                self,
                server,
                path=path if path != "/mcp" else "/sse",
                message_path=message_path,
                auth=auth,
                allowed_origins=(
                    frozenset(allowed_origins) if allowed_origins is not None else None
                ),
                exclude_middleware=exclude_middleware,
            )
            return None

        raise ValueError(
            f"Unsupported MCP transport {transport!r}; supported transports are "
            "'stdio', 'http', and 'sse' (the deprecated split-endpoint wire)."
        )
