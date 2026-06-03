"""MCPServer - dispatch JSON-RPC 2.0 method calls against the tool registry.

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

from veloce._internal import _is_async_callable
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.plan_bridge import bind_arguments
from veloce.contrib.mcp.registry import ToolRegistry, build_registry
from veloce.dependency import DependencyResolver
from veloce.exceptions import RequestValidationError
from veloce.http.response import Response
from veloce.instrumentation import RequestMetrics

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.registry import MCPTool

_logger = logging.getLogger(__name__)

# Model Context Protocol revision this server speaks. Sent back in the
# ``initialize`` result so a client can confirm compatibility.
PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC 2.0 error codes (Sec. 5.1) plus the MCP "method not found" reuse.
_JSONRPC_PARSE_ERROR = -32700
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
            # A handler error is a tool-level error, surfaced in the result
            # (isError=true) rather than a JSON-RPC transport error, so the
            # agent can read the message. MCP spec: tool errors live in-band.
            await self._instrument(tool, started)
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }
        await self._instrument(tool, started)
        shaped = self._shape_result(tool, result)
        return {"content": [{"type": "text", "text": _stringify(shaped)}]}

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
        """Resolve DI and call the handler, draining teardowns afterwards."""
        context = MCPContext(tool.name, arguments)
        resolver = DependencyResolver()
        resolver._overrides = self.app._dependency_overrides
        resolver._override_subplans = self.app._override_subplans
        exc: BaseException | None = None
        try:
            # Only the argument-binding boundary maps a malformed argument to an
            # invalid-params transport error: a missing argument (TypeError), a
            # failed type coercion (RequestValidationError, raised by the shared
            # coercion helper) or a failed model validation (ValueError). The
            # handler call lives outside this guard so any exception raised in
            # the handler body - including a genuine TypeError / ValueError -
            # propagates and is surfaced as an in-band isError result by
            # `_tools_call`, never leaked onto the JSON-RPC error channel.
            try:
                kwargs, request = await bind_arguments(
                    tool.plan, arguments, context, resolver, tool.route_dep_plans
                )
            except (TypeError, ValueError, RequestValidationError) as err:
                raise _ToolInputError(str(err)) from err

            handler = tool.handler
            if _is_async_callable(handler):
                result = await handler(**kwargs)
            else:
                # A sync handler runs in the thread pool so it cannot block the
                # event loop - the same offload the HTTP path applies, with the
                # current context copied so contextvars stay readable.
                loop = asyncio.get_running_loop()
                ctx = contextvars.copy_context()
                result = await loop.run_in_executor(
                    None, ctx.run, functools.partial(handler, **kwargs)
                )

            # Run any background tasks the handler scheduled, mirroring the HTTP
            # path's post-handler execution. The stdio path has no response to
            # return to first, so they are awaited inline; a task error is
            # logged, never allowed to fail the (already-produced) tool result.
            tasks = request._background_tasks
            if tasks is not None:
                try:
                    await tasks.run_all()
                except Exception:
                    _logger.exception("MCP background task failed")
            return result
        except BaseException as err:  # noqa: BLE001 - re-raised after teardown
            exc = err
            raise
        finally:
            await resolver.run_teardowns(exc)

    async def _instrument(self, tool: MCPTool, started: float) -> None:
        """Fire the app instrumentation hooks for a finished tool call.

        Reuses the same `RequestMetrics`/`add_instrumentation` contract the
        HTTP path uses: `method` is the JSON-RPC method, `route` is the tool
        name (a low-cardinality label), `path` the tool name too.
        """
        hooks = self.app._instrumentation
        if not hooks:
            return
        duration_ms = (time.perf_counter() - started) * 1000.0
        metrics = RequestMetrics(
            method="tools/call",
            path=tool.name,
            route=tool.name,
            status_code=200,
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


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _response_body_value(response: Response) -> Any:
    """Unwrap a Response into the value `_stringify` should serialise.

    A JSON-typed body is decoded back to a Python value so the tool result
    carries the same JSON the HTTP client would receive; any other body decodes
    to its text. The body bytes are the already-rendered response body, so no
    further response-model work is needed.
    """
    body = response.body
    if not body:
        return ""
    if response.mimetype.endswith("json"):
        import orjson

        try:
            return orjson.loads(body)
        except orjson.JSONDecodeError:
            pass
    return body.decode("utf-8", "replace")


def _stringify(result: Any) -> str:
    """Serialise a handler return value to the text content of a tool result."""
    if isinstance(result, str):
        return result
    import orjson

    try:
        return orjson.dumps(result, default=_orjson_default).decode()
    except (TypeError, orjson.JSONEncodeError):
        return str(result)


def _orjson_default(value: Any) -> Any:
    """Fallback serialiser for values orjson cannot encode natively."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)
