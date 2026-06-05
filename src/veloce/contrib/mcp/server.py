"""MCPServer — dispatch JSON-RPC 2.0 method calls against the tool registry.

The server is transport-agnostic: a transport (stdio in v1) hands it decoded
JSON-RPC request objects and forwards the responses it returns. It implements
the three Model Context Protocol methods v1 needs - ``initialize``,
``tools/list``, and ``tools/call`` - and nothing more. A ``tools/call`` runs
the handler through the shared `DependencyResolver`, so `Depends()` graphs,
`yield`-style teardown, and `Security` all behave exactly as on the HTTP and
WebSocket paths. Per-tool instrumentation fires through the same
`app.add_instrumentation` hook the request path uses.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import time
from typing import TYPE_CHECKING, Any

import orjson

from veloce import status
from veloce._internal import _is_async_callable, is_json_mimetype
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.plan_bridge import _build_request, bind_arguments
from veloce.contrib.mcp.registry import ToolRegistry, build_registry
from veloce.dependency import DependencyResolver
from veloce.exceptions import RequestValidationError
from veloce.helpers import _current_app_var, _current_request_var, g
from veloce.http.response import Response
from veloce.instrumentation import RequestMetrics
from veloce.routing.router import RouteMatch

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.registry import MCPTool

_logger = logging.getLogger(__name__)

# Model Context Protocol revision this server speaks. Sent back in the
# ``initialize`` result so a client can confirm compatibility.
PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 2.0 error codes (Sec. 5.1) plus the MCP "method not found" reuse.
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603


class MCPServer:
    """Serve a Veloce app's MCP tools over JSON-RPC 2.0.

    Build once with the app; the registry is assembled eagerly so a
    registration-time safety violation (missing description, duplicate name)
    surfaces before any client connects.
    """

    __slots__ = ("app", "registry", "server_name", "server_version")

    def __init__(self, app: Any, registry: ToolRegistry | None = None) -> None:
        self.app = app
        self.registry = registry if registry is not None else build_registry(app)
        self.server_name = getattr(app, "title", None) or "Veloce"
        self.server_version = getattr(app, "version", None) or "0.1.0"

    # -- JSON-RPC dispatch ------------------------------------------

    async def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one decoded JSON-RPC request; return the response object.

        Returns `None` for a notification (a request with no ``id``), which
        carries no response per JSON-RPC 2.0 Sec. 4.1.
        """
        msg_id = message.get("id")
        method = message.get("method")

        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _error(msg_id, _JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")

        params = message.get("params") or {}
        is_notification = "id" not in message

        try:
            if method == "initialize":
                result = self._initialize()
            elif method == "notifications/initialized":
                # Client handshake ack - a notification, no response.
                return None
            elif method == "tools/list":
                result = self._tools_list()
            elif method == "tools/call":
                result = await self._tools_call(params)
            else:
                if is_notification:
                    return None
                return _error(msg_id, _JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")
        except _ToolInputError as exc:
            return _error(msg_id, _JSONRPC_INVALID_PARAMS, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            _logger.exception("MCP method %s raised", method)
            return _error(msg_id, _JSONRPC_INTERNAL_ERROR, str(exc))

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    # -- Method handlers --------------------------------------------

    def _initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.server_name, "version": self.server_version},
        }

    def _tools_list(self) -> dict[str, Any]:
        tools = [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in self.registry.tools.values()
        ]
        return {"tools": tools}

    async def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise _ToolInputError("tools/call requires a string 'name'")
        tool = self.registry.get(name)
        if tool is None:
            raise _ToolInputError(f"Unknown tool: {name}")

        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _ToolInputError("tools/call 'arguments' must be an object")

        started = time.perf_counter()
        try:
            result = await self._invoke(tool, arguments)
        except _ToolInputError:
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
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }

        # A `before_request` / middleware short-circuit or a route-backed tool's
        # final `Response` carries the real status code (an auth 401, a 500 from
        # an exception handler, a 200 success); instrumentation must report that,
        # not a hard-coded 200. The shaped result is derived from the same
        # response.
        if isinstance(result, (_ShortCircuit, _RouteResponse)):
            response = result.response
            await self._instrument(tool, started, response.status_code)
            return self._result_from_response(tool, response)

        try:
            shaped = self._shape_result(tool, result)
        except _StreamingNotSupported as exc:
            # A streamed / SSE response carries no buffered body for v1 to
            # serialise; surface the limitation in-band rather than returning
            # an empty result. The unsupported-streaming case is an in-band
            # error, recorded as a 500.
            await self._instrument(tool, started, status.HTTP_500_INTERNAL_SERVER_ERROR)
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        # A pure tool's raw return that completed without error is a genuine 200.
        await self._instrument(tool, started, status.HTTP_200_OK)
        return {"content": [{"type": "text", "text": _stringify(shaped)}]}

    def _result_from_response(self, tool: MCPTool, response: Response) -> dict[str, Any]:
        """Shape a route-backed tool's `Response` into the MCP call result.

        The response body is decoded back to a value (so the agent sees the same
        JSON the HTTP client would) and a 4xx/5xx status is flagged as an in-band
        `isError`. A streamed / SSE response carries no buffered body, so the
        limitation is surfaced in-band rather than yielding an empty result.
        """
        try:
            shaped = self._shape_result(tool, response)
        except _StreamingNotSupported as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        content: dict[str, Any] = {"content": [{"type": "text", "text": _stringify(shaped)}]}
        if response.status_code >= 400:
            content["isError"] = True
        return content

    def _shape_result(self, tool: MCPTool, result: Any) -> Any:
        """Run a route-derived tool's return through the HTTP response shaping.

        A pure `@app.mcp_tool` (no route) returns its value unchanged. For a
        tool exposed from an HTTP route the handler return is shaped exactly as
        the HTTP path shapes it: the route `response_model` filtering runs first
        (so fields hidden on the HTTP response cannot leak over MCP), and a
        returned `Response`/`JSONResponse` is unwrapped to its actual body - a
        JSON body decoded back to a value, any other body to its text - rather
        than serialised as an object repr.
        """
        route_info = tool.route_info
        if route_info is None:
            return result

        # `response_model` reshapes only a non-`Response` return, mirroring
        # `app._build_response`: a handler that built its own Response already
        # chose its body.
        if route_info.response_model is not None and not isinstance(result, Response):
            result = self.app._apply_response_model(result, route_info)

        if isinstance(result, Response):
            return _response_body_value(result)
        return result

    # -- Invocation -------------------------------------------------

    async def _invoke(self, tool: MCPTool, arguments: dict[str, Any]) -> Any:
        """Resolve DI and call the handler, draining teardowns afterwards.

        The handler runs inside the same request-context binding the HTTP path
        uses: `current_app` and `request` are bound onto their contextvars and
        `g` is reset, so a handler or dependency that reads `current_app` / `g`
        works.

        For a route-derived tool the full request lifecycle is replayed so the
        tool result matches the HTTP response: the matched path parameters are
        copied onto `request.path_params`, the app's `before_request` chain runs
        first (a hook returning a `Response` short-circuits the call), the
        handler return is shaped through the route's `_build_response`, the
        `after_request` chain runs and may rewrite that response, and the
        `teardown_request` / `teardown_appcontext` hooks fire in the `finally`.
        A handler exception is routed through the app's exception handlers (the
        same lookup the HTTP path uses) and the resulting response becomes the
        tool result. A pure `@app.mcp_tool` (no route) has no such lifecycle and
        its return value is passed back unchanged.
        """
        context = MCPContext(tool.name, arguments)
        resolver = DependencyResolver()
        resolver._overrides = self.app._dependency_overrides
        resolver._override_subplans = self.app._override_subplans

        route_info = tool.route_info
        # Seed the synthetic request's value sources with the call arguments so
        # a sub-dependency `Query` / `Body` / `Header` / `Cookie` / `Form`
        # marker resolves from them, the same way a top-level tool parameter
        # does (see `_build_request`). A route-backed tool also adopts the
        # wrapped route's real HTTP method and rule path so anything branching
        # on `request.method` / `request.path` matches the HTTP path.
        if route_info is not None:
            request = _build_request(
                tool.name,
                arguments,
                method=tool.route_method,
                path=route_info.path_template or None,
            )
        else:
            request = _build_request(tool.name, arguments)
        request.app = self.app

        # Bind the request context exactly as `handle_request` does: the
        # `current_app` / `request` contextvars plus a fresh `g`. Letting the
        # contextvars fall through when the call ends is intentional - stdio
        # calls run sequentially, each rebinding before it reads.
        _current_app_var.set(self.app)
        _current_request_var.set(request)
        g._reset()

        exc: BaseException | None = None
        bp_name: str | None = None

        # For a pure `@app.mcp_tool` there is no route lifecycle to replay - run
        # the handler with its DI graph and return the raw value, draining only
        # the yield-dependency teardowns. The exception path stays in-band.
        if route_info is None:
            try:
                return await self._invoke_pure(tool, arguments, context, resolver, request)
            except BaseException as err:  # noqa: BLE001 - re-raised after teardown
                exc = err
                raise
            finally:
                await resolver.run_teardowns(exc)

        # Route-derived tool: replay the matched-route state the HTTP path sets
        # before dispatch. `request.endpoint` and `url_rule` let a hook gate on
        # the route name and the blueprint bucket resolve; `path_params` carries
        # the tool arguments that name a route path parameter so a hook /
        # dependency / handler reading `request.path_params` sees them, exactly
        # as on the HTTP path.
        request.endpoint = route_info.name
        request._state["url_rule"] = route_info.path_template
        request.path_params = _route_path_params(route_info, arguments)

        try:
            # Request-phase middleware runs first, exactly as `_dispatch_request`
            # runs it before `before_request`, so a route depending on
            # middleware-populated state (a session loaded by `SessionMiddleware`,
            # a header set by a custom middleware) sees it over MCP too. A
            # middleware that short-circuits by returning a `Response` is treated
            # like a `before_request` short-circuit: shaped into the tool result
            # and run through the same teardown `finally`. Returned from *inside*
            # this `try` so DI + `teardown_request` / `teardown_appcontext` still
            # fire. The response middleware phase is intentionally not replayed:
            # the tool result is derived from the response body, not a wire
            # response, so a response-mutating middleware (compression, headers)
            # has nothing to act on.
            # A route declaring `exclude_middleware` must skip the excluded
            # middleware here too, so the MCP path matches HTTP dispatch (which
            # runs the route's filtered chain). `_route_middleware_chains`
            # returns `None` when the route excludes nothing - the common,
            # zero-cost case - in which case the full app chain runs.
            filtered = (
                self.app._route_middleware_chains(route_info)
                if route_info.excluded_middleware is not None
                else None
            )
            request_chain = filtered[0] if filtered is not None else None
            early = await self.app._run_request_middleware(request, request_chain)
            if early is not None:
                return _ShortCircuit(early)

            # `before_request` (app-level then matched blueprint). A
            # short-circuit response is the tool result; `bp_name` is recorded
            # so the matched blueprint's `after_request` / teardown hooks fire
            # even on short-circuit. The short-circuit returns from *inside* this
            # `try` so the `finally` still drains DI teardowns and runs
            # `teardown_request` / `teardown_appcontext` - the HTTP dispatch runs
            # its teardown even when `before_request` returns early, and a tool
            # that relies on `teardown_request` cleanup on a rejected call (an
            # auth 401 short-circuit) must get the same.
            early, bp_name = await self.app._run_before_hooks(request)
            if early is not None:
                return _ShortCircuit(early)

            # URL value preprocessors run after `before_request` and after
            # `path_params` is populated, exactly as the HTTP path runs them in
            # `_resolve_route`, so a processor that rewrites a path param or
            # seeds `g` (locale / tenant extraction) is observed by the
            # dependencies and the handler.
            self.app._run_url_value_preprocessors(route_info.name, request.path_params)

            result = await self._bind_and_call(tool, arguments, context, resolver, request)

            # Shape the handler return into the final `Response` exactly as the
            # HTTP path does (`_build_response` runs the route `response_model`
            # filtering + coercion + injected-response merge), then run the
            # `after_request` chain so a hook can rewrite the response before the
            # tool result is derived from it - mirroring HTTP dispatch order.
            match = RouteMatch(route_info, request.path_params)
            response = self.app._build_response(request, match, result)
            response = await self.app._run_after_hooks(request, response, bp_name)

            # Background work: the handler's injected queue plus any task it
            # attached to its own `Response`. Awaited inline (the stdio path has
            # no response to flush first); a task error is logged, never allowed
            # to fail the produced tool result.
            tasks = request._background_tasks
            if tasks is not None:
                try:
                    await tasks.run_all()
                except Exception:
                    _logger.exception("MCP background task failed")
            await self._run_response_background(response)
            return _RouteResponse(response)
        except _ToolInputError:
            # A malformed argument is a transport-level invalid-params error,
            # not a handled application failure - re-raise so `_tools_call`
            # surfaces it on the JSON-RPC error channel. It still flows through
            # the `finally` so teardowns run.
            raise
        except BaseException as err:  # noqa: BLE001 - re-raised / routed after teardown
            exc = err
            # Route the handler exception through the app's exception handlers
            # (the same status-code + class lookup the HTTP path uses) so a
            # route relying on `@app.exception_handler(...)` - or the default
            # `HTTPException` JSON body - yields the right MCP payload. A
            # `BaseException` that is not an `Exception` (e.g. cancellation)
            # has no handler path and is re-raised after teardown.
            if isinstance(err, Exception):
                response = await self.app.handle_user_exception(err, request=request)
                return _RouteResponse(response)
            raise
        finally:
            # Yield-dependency teardowns first (the resource was acquired before
            # the handler ran and must be released regardless of outcome), then
            # the `teardown_request` / `teardown_appcontext` hooks - both receive
            # the exception (or None), mirroring the HTTP dispatch `finally`.
            await resolver.run_teardowns(exc)
            await self.app._run_request_teardown(exc, bp_name)

    async def _invoke_pure(
        self,
        tool: MCPTool,
        arguments: dict[str, Any],
        context: MCPContext,
        resolver: DependencyResolver,
        request: Any,
    ) -> Any:
        """Run a pure `@app.mcp_tool` handler and return its raw value.

        No route lifecycle applies, so the return value is passed back unchanged
        (the caller stringifies it) and a handler exception propagates to be
        surfaced in-band by `_tools_call`.
        """
        result = await self._bind_and_call(tool, arguments, context, resolver, request)
        tasks = request._background_tasks
        if tasks is not None:
            try:
                await tasks.run_all()
            except Exception:
                _logger.exception("MCP background task failed")
        await self._run_response_background(result)
        return result

    async def _bind_and_call(
        self,
        tool: MCPTool,
        arguments: dict[str, Any],
        context: MCPContext,
        resolver: DependencyResolver,
        request: Any,
    ) -> Any:
        """Bind the handler kwargs from `arguments` and call the handler.

        The argument-binding boundary is the only place that maps a malformed
        argument to an invalid-params transport error (`_ToolInputError`): a
        missing argument (TypeError), a failed coercion (RequestValidationError)
        or a failed model validation (ValueError). The handler call lives outside
        that guard so a genuine TypeError / ValueError raised in the handler body
        propagates unchanged - surfaced in-band for a pure tool, routed through
        the app's exception handlers for a route-backed one.
        """
        # Route rule `defaults=` fill handler kwargs the call did not supply,
        # matching HTTP precedence (explicit argument > route default > Python
        # default). A pure `@app.mcp_tool` has no route, hence no defaults.
        route_info = tool.route_info
        route_defaults = route_info.defaults if route_info is not None else None
        try:
            kwargs, _request = await bind_arguments(
                tool.plan,
                arguments,
                context,
                resolver,
                tool.route_dep_plans,
                request=request,
                route_defaults=route_defaults,
            )
        except (TypeError, ValueError, RequestValidationError) as err:
            raise _ToolInputError(str(err)) from err

        handler = tool.handler
        if _is_async_callable(handler):
            return await handler(**kwargs)
        # A sync handler runs in the thread pool so it cannot block the event
        # loop - the same offload the HTTP path applies, with the current context
        # copied so contextvars (`current_app` / `g` / the `request` proxy) stay
        # readable in the worker thread.
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        return await loop.run_in_executor(None, ctx.run, functools.partial(handler, **kwargs))

    @staticmethod
    async def _run_response_background(result: Any) -> None:
        """Run a returned `Response`'s `background` task, mirroring the HTTP path.

        The HTTP path schedules `response.background` (a `BackgroundTask` or a
        `BackgroundTasks` collection) in addition to the DI-injected queue.
        A task error is logged, never allowed to fail the produced tool result.
        """
        if not isinstance(result, Response):
            return
        background = result.background
        if background is None:
            return
        try:
            if hasattr(background, "run_all"):
                await background.run_all()
            elif hasattr(background, "run"):
                await background.run()
        except Exception:
            _logger.exception("MCP response background task failed")

    async def _instrument(self, tool: MCPTool, started: float, status_code: int) -> None:
        """Fire the app instrumentation hooks for a finished tool call.

        Reuses the same `RequestMetrics`/`add_instrumentation` contract the
        HTTP path uses: `method` is the JSON-RPC method, `route` is the tool
        name (a low-cardinality label), `path` the tool name too. `status_code`
        is the call's real outcome - the shaped `Response`'s status for a
        route-backed / short-circuited call, 500 for an unhandled handler error
        or an unsupported streaming result, 200 only on genuine success - so a
        4xx/5xx is never misreported as 200.
        """
        hooks = self.app._instrumentation
        if not hooks:
            return
        duration_ms = (time.perf_counter() - started) * 1000.0
        metrics = RequestMetrics(
            method="tools/call",
            path=tool.name,
            route=tool.name,
            status_code=status_code,
            duration_ms=duration_ms,
        )
        for hook in hooks:
            try:
                outcome = hook(metrics)
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception:
                _logger.exception("instrumentation hook raised an exception")


# -- Helpers ----------------------------------------------------------


class _ToolInputError(Exception):
    """A malformed tool call - reported as a JSON-RPC invalid-params error."""


class _StreamingNotSupported(Exception):
    """A route returned a streaming/SSE response, unsupported as an MCP tool (v1)."""


class _ShortCircuit:
    """A `before_request` hook's `Response`, returned in place of the handler call."""

    __slots__ = ("response",)

    def __init__(self, response: Response) -> None:
        self.response = response


class _RouteResponse:
    """A route-backed tool's final `Response` (shaped + after_request-rewritten,
    or built by an exception handler), returned in place of the raw value."""

    __slots__ = ("response",)

    def __init__(self, response: Response) -> None:
        self.response = response


def _route_path_params(route_info: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Build `request.path_params` for a route-backed tool call.

    The HTTP path fills `path_params` from the URL segments the router matched;
    a tool call has no URL, so the equivalent values arrive as named entries in
    the JSON `arguments`. Copy each argument whose name is one of the route's
    declared path parameters, then fill any route `defaults` not supplied, so a
    hook / dependency / handler that reads `request.path_params` sees the same
    mapping it would on the HTTP path.
    """
    params = {name: arguments[name] for name in route_info.param_names if name in arguments}
    for key, value in route_info.defaults.items():
        params.setdefault(key, value)
    return params


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _response_body_value(response: Response) -> Any:
    """Unwrap a Response into the value `_stringify` should serialise.

    A JSON-typed body is decoded back to a Python value so the tool result
    carries the same JSON the HTTP client would receive; any other body decodes
    to its text. The body bytes are the already-rendered response body, so no
    further response-model work is needed.

    A streaming / SSE response carries its payload on `_stream`, not `.body`, so
    v1 cannot serialise it; rather than silently yielding an empty result, this
    raises `_StreamingNotSupported`, surfaced as an in-band tool error.
    """
    if response.is_streamed:
        raise _StreamingNotSupported("streaming responses are not supported as MCP tools (v1)")
    body = response.body
    if not body:
        return ""
    if is_json_mimetype(response.mimetype):
        try:
            return orjson.loads(body)
        except orjson.JSONDecodeError:
            pass
    return body.decode("utf-8", "replace")


def _stringify(result: Any) -> str:
    """Serialise a handler return value to the text content of a tool result."""
    if isinstance(result, str):
        return result
    try:
        return orjson.dumps(result, default=_orjson_default).decode()
    except (TypeError, orjson.JSONEncodeError):
        return str(result)


def _orjson_default(value: Any) -> Any:
    """Fallback serialiser for values orjson cannot encode natively."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)
