"""MCPServer — dispatch JSON-RPC 2.0 method calls against the tool registry.

The server is transport-agnostic: a transport (stdio) hands it decoded JSON-RPC
request objects, forwards the responses it returns, and supplies the outbound sink
(`set_notifier`) the server pushes one-way notifications through. It implements
``initialize`` (negotiating the protocol version), ``ping``, the tool methods
(``tools/list`` / ``tools/call``), the resource methods (``resources/list`` /
``resources/templates/list`` / ``resources/read``), the prompt methods
(``prompts/list`` / ``prompts/get``), ``logging/setLevel``, and the
``notifications/initialized`` ack. A ``tools/call`` runs the handler through the
shared `DependencyResolver`, so `Depends()` graphs, `yield`-style teardown, and
`Security` all behave exactly as on the HTTP and WebSocket paths; resource reads
and prompt renders replay the same invocation path. Per-tool instrumentation fires
through the same `app.add_instrumentation` hook the request path uses.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import orjson

from veloce import status
from veloce._internal import _is_async_callable, is_json_mimetype, offload
from veloce.contrib.mcp.context import _LOG_RANKS, MCPContext
from veloce.contrib.mcp.plan_bridge import _build_request, bind_arguments
from veloce.contrib.mcp.prompts import MCPPrompt, PromptRegistry, build_prompt_registry
from veloce.contrib.mcp.registry import ToolRegistry, build_registry
from veloce.contrib.mcp.resources import MCPResource, ResourceRegistry, build_resource_registry
from veloce.dependency import DependencyResolver
from veloce.exceptions import RequestValidationError
from veloce.helpers import _current_app_var, _current_request_var, g
from veloce.http.response import Response
from veloce.instrumentation import RequestMetrics
from veloce.routing.router import RouteMatch

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.registry import MCPTool

_logger = logging.getLogger(__name__)

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

# JSON-RPC 2.0 error codes (Sec. 5.1) plus the MCP "method not found" reuse.
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603

# MCP "Resource not found" code (server-defined, outside the JSON-RPC reserved
# range), returned when ``resources/read`` names a URI the registry cannot
# resolve or whose route answers 404.
_JSONRPC_RESOURCE_NOT_FOUND = -32002

# The current call's outbound notification sink, scoped per request so a handler's
# progress / log notifications reach the right client. A ContextVar (not an
# instance attribute) keeps concurrent calls isolated on the Streamable HTTP
# transport, where many requests share one `MCPServer`; the serial stdio transport
# sets it once per serve task. `None` means no channel is wired (off-transport).
_notifier_var: ContextVar[Callable[[dict[str, Any]], Awaitable[None]] | None] = ContextVar(
    "_mcp_notifier", default=None
)


class MCPServer:
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
        "server_name",
        "server_version",
        "_call_timeout",
        "_log_level",
    )

    def __init__(
        self,
        app: Any,
        registry: ToolRegistry | None = None,
        resources: ResourceRegistry | None = None,
        prompts: PromptRegistry | None = None,
    ) -> None:
        self.app = app
        self.registry = registry if registry is not None else build_registry(app)
        self.resources = resources if resources is not None else build_resource_registry(app)
        self.prompts = prompts if prompts is not None else build_prompt_registry(app)
        self.server_name = getattr(app, "title", None) or "Veloce"
        self.server_version = getattr(app, "version", None) or "0.1.0"
        # Optional per-call wall-clock budget (`MCP_CALL_TIMEOUT` seconds in
        # `app.config`). The stdio serve loop is serial, so a handler that awaits
        # forever wedges every later call; when set, a call exceeding the budget
        # is cancelled and surfaced as an in-band tool error. `None` disables it.
        config = getattr(app, "config", None)
        self._call_timeout = config.get("MCP_CALL_TIMEOUT") if config is not None else None
        # The client's `logging/setLevel` minimum; `None` until set (emit all).
        self._log_level: str | None = None

    @staticmethod
    def set_notifier(notifier: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Wire the current context's outbound one-way notification sink.

        Sets the per-request `_notifier_var`; the stdio transport calls this once
        in its serve task, while the Streamable HTTP transport sets the var per
        request so concurrent calls never cross notifications.
        """
        _notifier_var.set(notifier)

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
                result = self._initialize(params)
            elif method == "notifications/initialized":
                # Client handshake ack - a notification, no response.
                return None
            elif method == "ping":
                # Base liveness utility either side may send; the spec'd reply is
                # an empty result object.
                result = {}
            elif method == "tools/list":
                result = self._tools_list()
            elif method == "tools/call":
                result = await self._tools_call(params)
            elif method == "resources/list":
                result = self._resources_list()
            elif method == "resources/templates/list":
                result = self._resource_templates_list()
            elif method == "resources/read":
                result = await self._resources_read(params)
            elif method == "prompts/list":
                result = self._prompts_list()
            elif method == "prompts/get":
                result = await self._prompts_get(params)
            elif method == "logging/setLevel":
                result = self._set_log_level(params)
            else:
                if is_notification:
                    return None
                return _error(msg_id, _JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")
        except _ToolInputError as exc:
            return _error(msg_id, _JSONRPC_INVALID_PARAMS, str(exc))
        except _ResourceError as exc:
            return _error(msg_id, exc.code, str(exc))
        except asyncio.TimeoutError:
            # A resources/read or prompts/get that overran the per-call budget
            # (a tools/call surfaces its own timeout in-band before here).
            return _error(msg_id, _JSONRPC_INTERNAL_ERROR, "request exceeded the MCP call timeout")
        except Exception as exc:  # pragma: no cover - defensive
            _logger.exception("MCP method %s raised", method)
            return _error(msg_id, _JSONRPC_INTERNAL_ERROR, self._error_text(exc, "internal error"))

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    # -- Method handlers --------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        # Echo the client's requested revision when supported; otherwise return
        # the server's latest, leaving the client to decide whether to proceed.
        requested = params.get("protocolVersion")
        version = (
            requested
            if isinstance(requested, str) and requested in _SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        # `tools` is always advertised; `resources` only when the app exposes at
        # least one, so a client does not probe a primitive this server has
        # nothing to serve for. `subscribe`/`listChanged` are off - resources are
        # served on demand, with no update notifications on the serial loop.
        # `logging` is always advertised: any tool may emit a log message through
        # `MCPContext.log`, and the client may raise the minimum with
        # ``logging/setLevel``.
        capabilities: dict[str, Any] = {"tools": {"listChanged": False}, "logging": {}}
        if self.resources.resources:
            capabilities["resources"] = {"subscribe": False, "listChanged": False}
        if self.prompts.prompts:
            capabilities["prompts"] = {"listChanged": False}
        return {
            "protocolVersion": version,
            "capabilities": capabilities,
            "serverInfo": {"name": self.server_name, "version": self.server_version},
        }

    def _tools_list(self) -> dict[str, Any]:
        return {"tools": [self._describe_tool(tool) for tool in self.registry.tools.values()]}

    @staticmethod
    def _describe_tool(tool: MCPTool) -> dict[str, Any]:
        """Shape one registered tool into its `tools/list` entry.

        Beyond the required `name` / `description` / `inputSchema`, a
        route-backed tool carries a human-readable `title` (its route summary),
        HTTP-derived `annotations` (read-only / idempotent / destructive hints),
        and an `outputSchema` when its result has a declared object shape.
        """
        entry: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }
        title = tool.route_info.summary if tool.route_info is not None else None
        if title:
            entry["title"] = title
        annotations = _tool_annotations(tool.route_methods)
        if annotations is not None:
            entry["annotations"] = annotations
        if tool.output_schema is not None:
            entry["outputSchema"] = tool.output_schema
        return entry

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

        progress_token = _progress_token(params)
        started = time.perf_counter()
        try:
            result = await self._run_invoke(tool, arguments, progress_token)
        except _ToolInputError:
            raise
        except asyncio.TimeoutError:
            await self._instrument(tool, started, status.HTTP_504_GATEWAY_TIMEOUT)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"tool call exceeded the {self._call_timeout}s timeout",
                    }
                ],
                "isError": True,
            }
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
                "content": [
                    {
                        "type": "text",
                        "text": self._error_text(exc, "the tool raised an internal error"),
                    }
                ],
                "isError": True,
            }

        # A `before_request` / middleware short-circuit or a route-backed tool's
        # final `Response` carries the real status code (an auth 401, a 500 from
        # an exception handler, a 200 success); instrumentation must report that,
        # not a hard-coded 200. The shaped result is derived from the same
        # response.
        if isinstance(result, (_ShortCircuit, _RouteResponse)):
            response = result.response
            try:
                await self._drain_stream(response)
            except (_StreamTooLarge, _StreamTimeout) as exc:
                await self._instrument(tool, started, status.HTTP_500_INTERNAL_SERVER_ERROR)
                return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
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
        except (_StreamTooLarge, _StreamTimeout) as exc:
            await self._instrument(tool, started, status.HTTP_500_INTERNAL_SERVER_ERROR)
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
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
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": ("tool result does not conform to the declared output schema"),
                        }
                    ],
                    "isError": True,
                }
        return self._success_result(tool, shaped)

    async def _drain_stream(self, response: Response) -> None:
        """Buffer a streamed response into its body so it can be a tool result.

        A `StreamingResponse` / `EventSourceResponse` has no single body, but an
        MCP `tools/call` returns one result, so the stream is consumed and joined
        into the response body; afterwards it shapes like any buffered response.
        A non-streaming response is left untouched. Draining is bounded in both
        size and time: a stream exceeding `_STREAM_BUFFER_LIMIT` raises
        `_StreamTooLarge`, and one that has not completed within
        `_STREAM_DRAIN_TIMEOUT` raises `_StreamTimeout` - both surfaced as an
        in-band error. The size cap stops a fast runaway; the deadline stops a
        slow or never-completing stream (a heartbeat SSE feed, a handler that
        awaits forever) from wedging the serial stdio serve loop. The size cap
        bounds the *accumulated* bytes - a single chunk is materialised by the
        producer before the check, so peak memory is the cap plus the largest
        single chunk. On either bounded failure - size cap or timeout - the
        underlying async generator is closed so the producing task does not leak,
        and that close is itself bounded by the same deadline.
        """
        if not response.is_streamed:
            return
        chunks: list[bytes] = []
        stream = response.iter_encoded()

        async def _collect() -> None:
            total = 0
            async for chunk in stream:
                piece = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                total += len(piece)
                if total > _STREAM_BUFFER_LIMIT:
                    raise _StreamTooLarge(
                        f"streamed result exceeded the {_STREAM_BUFFER_LIMIT}-byte MCP buffer limit"
                    )
                chunks.append(piece)

        # The collect runs as its own task so the deadline can be enforced
        # independently of the producer's teardown. Cancelling an in-flight
        # `async for` step runs the generator's `finally` as part of that
        # cancellation; if that teardown awaits (a malicious / buggy
        # `finally: await ...`), awaiting the cancellation to completion would
        # re-wedge the serial stdio serve loop past the deadline. So the
        # cancellation is itself bounded and the producer is abandoned if its
        # teardown overruns the budget - the timeout's whole purpose.
        task = asyncio.ensure_future(_collect())
        try:
            # `wait_for` (not `asyncio.timeout`, which is 3.11+) is fed a shield
            # so a timeout does not synchronously await the cancellation here.
            await asyncio.wait_for(asyncio.shield(task), _STREAM_DRAIN_TIMEOUT)
        except asyncio.TimeoutError as exc:
            await self._abandon_drain(task, stream)
            raise _StreamTimeout(
                f"streamed result did not complete within the "
                f"{_STREAM_DRAIN_TIMEOUT}-second MCP drain timeout"
            ) from exc
        except _StreamTooLarge:
            # The size cap raises from inside the task, leaving the producer
            # suspended at its `async for`; close it under the same bound so it
            # does not leak until GC.
            await self._abandon_drain(task, stream)
            raise
        response.body = b"".join(chunks)
        response._stream = None

    @staticmethod
    async def _abandon_drain(task: asyncio.Future[None], stream: Any) -> None:
        """Tear down an aborted drain's collect task and producer, bounded.

        Both the task cancellation and the generator `aclose()` are bounded by
        `_STREAM_DRAIN_TIMEOUT`; a teardown that itself awaits past the budget is
        abandoned rather than allowed to re-wedge the serial stdio serve loop.
        """
        # On the timeout path the task is still running its in-flight step and is
        # cancelled here; on the size-cap path it has already raised and only the
        # suspended producer needs closing.
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), _STREAM_DRAIN_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                _logger.exception("MCP stream drain teardown failed")
        aclose = getattr(stream, "aclose", None)
        if aclose is None:
            return
        try:
            await asyncio.wait_for(aclose(), _STREAM_DRAIN_TIMEOUT)
        except Exception:
            _logger.exception("MCP stream cleanup failed")

    def _result_from_response(
        self, tool: MCPTool, response: Response, model_filtered: bool
    ) -> dict[str, Any]:
        """Shape a route-backed tool's `Response` into the MCP call result.

        The response body is decoded back to a value (so the agent sees the same
        JSON the HTTP client would) and a 4xx/5xx status is flagged as an in-band
        `isError`. A streamed response has already been buffered into its body by
        `_drain_stream` before this runs.

        `model_filtered` is `True` when the route built this response from a
        non-`Response` handler return (so `_build_response` ran the
        `response_model` filter over it) and `False` when the handler returned
        its own `Response` (whose body bypassed the filter). When `False` and the
        route declares a `response_model`, the decoded body is re-run through that
        filter here before being emitted as `structuredContent`, so the value
        conforms to the advertised `outputSchema` and a field the model would
        exclude cannot leak under a schema that says it is absent.
        """
        # An image/audio body has no text form; emit it as the matching typed
        # MCP content block (base64) rather than a decoded-text block.
        binary = _binary_result(response)
        if binary is not None:
            return binary
        shaped = self._shape_result(tool, response)
        # A 4xx/5xx is an in-band error: surface the body text and flag it,
        # without structured content (the error body is not the tool's output
        # shape). A success goes through the shared success shaping so a declared
        # `outputSchema` yields `structuredContent` alongside the text block.
        if response.status_code >= 400:
            return {"content": [{"type": "text", "text": _stringify(shaped)}], "isError": True}
        # A handler that returned its own `Response` bypassed the route
        # `response_model` filter, so its decoded body is not yet trusted to
        # conform to the advertised `outputSchema`. Re-run it through the filter
        # here so the emitted `structuredContent` both honours the MCP MUST (a
        # declared `outputSchema` is matched by conforming structured output) and
        # keeps the field-leak protection - a field the model excludes is dropped
        # before it reaches the client.
        #
        # The body is one the handler explicitly built, so it may not be an
        # object the `response_model` can validate at all: a `PlainTextResponse`,
        # an SSE / streaming body decoded to text, or a JSON shape that fails
        # `model_validate`. The HTTP path serves such a body as-is (a handler's
        # own `Response` bypasses `response_model` there), so the re-filter must
        # never harden a call HTTP would serve into a JSON-RPC transport error.
        # On any re-filter failure the value is emitted as the text content block
        # only, with no `structuredContent` - a non-object body has no object
        # form to advertise anyway.
        route_info = tool.route_info
        if tool.output_schema is not None:
            if route_info is not None and route_info.response_model is not None:
                # `_build_response` already ran the `response_model` filter for a
                # non-`Response` return (`model_filtered`); only a handler-built
                # `Response` body still needs it, through the route's dump
                # settings so `structuredContent` conforms to the advertised
                # schema and an excluded field cannot leak.
                if not model_filtered:
                    try:
                        shaped = self.app._apply_response_model(shaped, route_info)
                    except Exception:
                        return {"content": [{"type": "text", "text": _stringify(shaped)}]}
            elif tool.output_model is not None:
                # The output schema came from the handler's return annotation, not
                # a `response_model`, so `_build_response` never filtered to it -
                # validate every return (raw value or handler-built body) through
                # the model so a field outside it cannot leak.
                try:
                    shaped = tool.output_model.model_validate(shaped).model_dump(mode="json")
                except Exception:
                    return {"content": [{"type": "text", "text": _stringify(shaped)}]}
        return self._success_result(tool, shaped)

    def _success_result(self, tool: MCPTool, shaped: Any) -> dict[str, Any]:
        """Build a successful tool-call result from a shaped return value.

        The text content block is always present (back-compatible with clients
        that read only `content`). `structuredContent` is added when the tool
        declares an `outputSchema` and the value carries an object form. A body
        that bypassed the route `response_model` has already been re-filtered by
        the caller, so any value reaching here is trusted to conform to the
        advertised schema - honouring the MCP requirement that a declared
        `outputSchema` is matched by conforming structured output.
        """
        result: dict[str, Any] = {"content": [{"type": "text", "text": _stringify(shaped)}]}
        if tool.output_schema is not None:
            structured = _to_structured(shaped)
            if structured is not None:
                result["structuredContent"] = structured
        return result

    def _shape_result(self, tool: MCPTool, result: Any) -> Any:
        """Run a tool's return through the HTTP response shaping.

        For a tool exposed from an HTTP route the handler return is shaped
        exactly as the HTTP path shapes it: the route `response_model` filtering
        runs first for a non-`Response` return (so fields hidden on the HTTP
        response cannot leak over MCP). A returned `Response`/`JSONResponse` -
        from either a route-backed or a pure `@app.mcp_tool` - is unwrapped to
        its actual body (a JSON body decoded back to a value, any other body to
        its text) rather than serialised as an object repr; a streamed response
        has been buffered into that body by `_drain_stream` first. A pure tool's
        non-`Response` return is passed back unchanged.

        This shapes the value only. A route-backed handler that returned its own
        `Response` bypassed the `response_model` filter, so its decoded body is
        re-run through that filter by the caller (`_result_from_response`) before
        being advertised as schema-conformant `structuredContent`.
        """
        route_info = tool.route_info

        # `response_model` reshapes only a non-`Response` return, mirroring
        # `app._build_response`: a handler that built its own Response already
        # chose its body.
        if (
            route_info is not None
            and route_info.response_model is not None
            and not isinstance(result, Response)
        ):
            result = self.app._apply_response_model(result, route_info)

        if isinstance(result, Response):
            return _response_body_value(result)
        return result

    # -- Resources --------------------------------------------------

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
            raise _ToolInputError("resources/read requires a string 'uri'")
        matched = self.resources.match(uri)
        if matched is None:
            raise _ResourceError(_JSONRPC_RESOURCE_NOT_FOUND, f"Unknown resource: {uri}")
        resource, arguments = matched

        try:
            result = await self._run_invoke(resource.tool, arguments, _progress_token(params))
        except _ToolInputError as exc:
            # A path-parameter value the URI carries that the route cannot coerce
            # (a non-int `{user_id}`) is an invalid-params read, not a 404.
            raise _ResourceError(_JSONRPC_INVALID_PARAMS, str(exc)) from exc

        # A resource is always route-backed, so `_invoke` yields a
        # `_RouteResponse` (or a `_ShortCircuit` from a middleware / before_request
        # guard); both carry the `Response` whose body is the resource contents.
        response = result.response if isinstance(result, (_ShortCircuit, _RouteResponse)) else None
        if response is None:
            raise _ResourceError(_JSONRPC_INTERNAL_ERROR, f"Resource {uri} produced no response")
        try:
            await self._drain_stream(response)
        except (_StreamTooLarge, _StreamTimeout) as exc:
            raise _ResourceError(_JSONRPC_INTERNAL_ERROR, str(exc)) from exc
        if response.status_code >= 400:
            code = (
                _JSONRPC_RESOURCE_NOT_FOUND
                if response.status_code == status.HTTP_404_NOT_FOUND
                else _JSONRPC_INTERNAL_ERROR
            )
            raise _ResourceError(code, _stringify(_response_body_value(response)))
        return {"contents": [_resource_contents(uri, response)]}

    # -- Prompts ----------------------------------------------------

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
            raise _ToolInputError("prompts/get requires a string 'name'")
        prompt = self.prompts.get(name)
        if prompt is None:
            raise _ToolInputError(f"Unknown prompt: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _ToolInputError("prompts/get 'arguments' must be an object")

        result = await self._run_invoke(prompt.tool, arguments, _progress_token(params))
        out: dict[str, Any] = {"messages": _normalize_prompt_messages(result)}
        if prompt.description:
            out["description"] = prompt.description
        return out

    # -- Logging ----------------------------------------------------

    def _set_log_level(self, params: dict[str, Any]) -> dict[str, Any]:
        """Set the minimum level for ``notifications/message`` (logging/setLevel)."""
        level = params.get("level")
        if not isinstance(level, str) or level not in _LOG_RANKS:
            raise _ToolInputError("logging/setLevel requires a valid RFC 5424 'level'")
        self._log_level = level
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

    # -- Invocation -------------------------------------------------

    async def _run_invoke(
        self, tool: MCPTool, arguments: dict[str, Any], progress_token: str | int | None
    ) -> Any:
        """Invoke a tool, applying the optional per-call timeout.

        With no `MCP_CALL_TIMEOUT` configured the handler runs unbounded (the
        common case, zero overhead); otherwise it is cancelled past the budget and
        the `asyncio.TimeoutError` is surfaced by the caller (in-band for a tool
        call, a JSON-RPC error for a resource read or prompt render).
        """
        if self._call_timeout is None:
            return await self._invoke(tool, arguments, progress_token)
        return await asyncio.wait_for(
            self._invoke(tool, arguments, progress_token), self._call_timeout
        )

    async def _invoke(
        self, tool: MCPTool, arguments: dict[str, Any], progress_token: str | int | None = None
    ) -> Any:
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
        context = MCPContext(
            tool.name,
            arguments,
            notifier=_notifier_var.get(),
            progress_token=progress_token,
            log_level=self._log_level,
        )
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

            # `_build_response` runs the route `response_model` filter only over a
            # non-`Response` handler return; a handler that returned its own
            # `Response` keeps that body unfiltered. Record which case this is so
            # the server only advertises a filtered body as schema-conformant
            # `structuredContent`.
            model_filtered = not isinstance(result, Response)

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
            return _RouteResponse(response, model_filtered)
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
        # loop - the same offload the HTTP path applies; `offload` preserves
        # request-scoped ContextVars.
        return await offload(handler, **kwargs)

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
        or a stream that overran the buffer limit, 200 only on genuine success -
        so a 4xx/5xx is never misreported as 200.
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


# HTTP-method semantics mapped to MCP tool annotation hints. Read-only verbs do
# not modify state; idempotent verbs are safe to retry; a mutating verb that is
# not purely additive (PUT/PATCH/DELETE) is flagged destructive so a client can
# prompt for consent. These are advisory hints a client may ignore.
_READONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE"})
_NON_DESTRUCTIVE_METHODS = frozenset({"GET", "HEAD", "POST", "OPTIONS", "TRACE"})


def _tool_annotations(methods: list[str]) -> dict[str, Any] | None:
    """Derive MCP tool annotation hints from a route's HTTP methods.

    A multi-verb route is rated conservatively across every verb it serves:
    read-only and idempotent only when *all* verbs qualify, destructive when
    *any* verb is non-additive (so a `GET`+`DELETE` route is flagged
    destructive, not read-only). A pure `@app.mcp_tool` (no route) has no HTTP
    verb to map, so it carries no annotations.
    """
    if not methods:
        return None
    verbs = {method.upper() for method in methods}
    return {
        "readOnlyHint": verbs <= _READONLY_METHODS,
        "idempotentHint": verbs <= _IDEMPOTENT_METHODS,
        "destructiveHint": not (verbs <= _NON_DESTRUCTIVE_METHODS),
    }


def _to_structured(value: Any) -> dict[str, Any] | None:
    """Render a tool result as the JSON object MCP `structuredContent` requires.

    A mapping passes through; a Pydantic model is dumped in JSON mode. A
    non-object result (list, scalar) has no object form, so `None` is returned
    and only the text content block is sent.
    """
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, dict):
            return dumped
    return None


def _binary_result(response: Response) -> dict[str, Any] | None:
    """Shape an image/audio response body into a typed MCP content block.

    MCP defines first-class `image` and `audio` content blocks carrying the bytes
    as base64 with their media type. A body of either kind has no useful text
    form, so it is emitted as that typed block instead of a garbled decoded-text
    block; any other media type returns `None` and the caller shapes it as text /
    structured content as before. A 4xx/5xx still flags `isError` so an error is
    never read as a successful result.
    """
    mimetype = response.mimetype
    if mimetype.startswith("image/"):
        kind = "image"
    elif mimetype.startswith("audio/"):
        kind = "audio"
    else:
        return None
    block = {
        "type": kind,
        "data": base64.b64encode(response.body or b"").decode("ascii"),
        "mimeType": mimetype,
    }
    result: dict[str, Any] = {"content": [block]}
    if response.status_code >= 400:
        result["isError"] = True
    return result


def _describe_resource(resource: MCPResource) -> dict[str, Any]:
    """Shape a static resource into its `resources/list` entry."""
    entry: dict[str, Any] = {
        "uri": resource.uri,
        "name": resource.name,
        "description": resource.description,
    }
    if resource.title:
        entry["title"] = resource.title
    return entry


def _describe_resource_template(resource: MCPResource) -> dict[str, Any]:
    """Shape a template resource into its `resources/templates/list` entry."""
    entry: dict[str, Any] = {
        "uriTemplate": resource.uri,
        "name": resource.name,
        "description": resource.description,
    }
    if resource.title:
        entry["title"] = resource.title
    return entry


def _resource_contents(uri: str, response: Response) -> dict[str, Any]:
    """Shape a resource route's response body into one MCP resource-contents entry.

    A JSON or `text/*` body is returned as `text` (the value an agent reads); any
    other media type is returned as a base64 `blob`. The entry carries the read
    URI and the response's media type.
    """
    mimetype = response.mimetype
    body = response.body or b""
    entry: dict[str, Any] = {"uri": uri, "mimeType": mimetype}
    if is_json_mimetype(mimetype) or mimetype.startswith("text/"):
        entry["text"] = body.decode("utf-8", "replace")
    else:
        entry["blob"] = base64.b64encode(body).decode("ascii")
    return entry


# Valid MCP prompt message roles; an unrecognised role from a handler-built
# message falls back to "user".
_PROMPT_ROLES = frozenset({"user", "assistant"})


def _describe_prompt(prompt: MCPPrompt) -> dict[str, Any]:
    """Shape a prompt into its `prompts/list` entry."""
    entry: dict[str, Any] = {"name": prompt.name, "description": prompt.description}
    if prompt.arguments:
        entry["arguments"] = prompt.arguments
    return entry


def _user_text_message(text: str) -> dict[str, Any]:
    """Build a user-role MCP prompt message carrying a single text block."""
    return {"role": "user", "content": {"type": "text", "text": text}}


def _normalize_prompt_message(item: Any) -> dict[str, Any]:
    """Normalise one prompt message item into an MCP prompt message.

    A string is a user text message; a mapping is read as a ``{"role", "content"}``
    message whose string content is wrapped into a text content block and whose
    unrecognised role falls back to user. Any other item is stringified.
    """
    if isinstance(item, str):
        return _user_text_message(item)
    if isinstance(item, dict):
        role = item.get("role")
        content = item.get("content")
        if isinstance(content, str):
            content = {"type": "text", "text": content}
        return {"role": role if role in _PROMPT_ROLES else "user", "content": content}
    return _user_text_message(_stringify(item))


def _normalize_prompt_messages(result: Any) -> list[dict[str, Any]]:
    """Normalise a prompt callable's return into MCP prompt messages.

    A string becomes a single user text message; a list becomes one message per
    item; any other return is stringified into a single user message.
    """
    if isinstance(result, str):
        return [_user_text_message(result)]
    if isinstance(result, list):
        return [_normalize_prompt_message(item) for item in result]
    return [_user_text_message(_stringify(result))]


# Cap on the bytes buffered from a streamed tool result. A streamed response has
# no single body, so the MCP path drains it into one (`_drain_stream`); this
# bound stops a runaway or unbounded stream from exhausting memory - crossing it
# yields an in-band tool error instead.
_STREAM_BUFFER_LIMIT = 5 * 1024 * 1024

# Wall-clock budget for draining a streamed tool result. The size cap alone does
# not defend against a slow or never-completing stream that stays small (a
# heartbeat SSE feed, a handler awaiting forever): without a deadline such a
# stream wedges the serial stdio serve loop, blocking every later request.
# Crossing it closes the stream and yields an in-band tool error.
_STREAM_DRAIN_TIMEOUT = 30.0


class _ToolInputError(Exception):
    """A malformed tool call - reported as a JSON-RPC invalid-params error."""


class _ResourceError(Exception):
    """A ``resources/read`` failure, reported as a JSON-RPC error with `code`."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class _StreamTooLarge(Exception):
    """A streamed tool result exceeded the buffer limit - reported in-band."""


class _StreamTimeout(Exception):
    """A streamed tool result outran the drain deadline - reported in-band."""


class _ShortCircuit:
    """A `before_request` hook's `Response`, returned in place of the handler call."""

    __slots__ = ("response",)

    def __init__(self, response: Response) -> None:
        self.response = response


class _RouteResponse:
    """A route-backed tool's final `Response` (shaped + after_request-rewritten,
    or built by an exception handler), returned in place of the raw value.

    `model_filtered` records whether the route's `response_model` filter ran over
    the value this response carries: `True` when `_build_response` built it from
    a non-`Response` handler return, `False` when the handler returned its own
    `Response` (or an exception handler built it). The server uses it to decide
    whether the decoded body may be advertised as schema-conformant
    `structuredContent`.
    """

    __slots__ = ("response", "model_filtered")

    def __init__(self, response: Response, model_filtered: bool = False) -> None:
        self.response = response
        self.model_filtered = model_filtered


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


def _progress_token(params: dict[str, Any]) -> str | int | None:
    """Return the call's `_meta.progressToken`, or `None` when none was sent.

    A client opts into progress notifications by attaching a `progressToken` to a
    request's `_meta`; without one the server reports no progress (per the MCP
    progress utility).
    """
    meta = params.get("_meta")
    if isinstance(meta, dict):
        token = meta.get("progressToken")
        if isinstance(token, (str, int)) and not isinstance(token, bool):
            return token
    return None


def _response_body_value(response: Response) -> Any:
    """Unwrap a Response into the value `_stringify` should serialise.

    A JSON-typed body is decoded back to a Python value so the tool result
    carries the same JSON the HTTP client would receive; any other body decodes
    to its text. The body bytes are the already-rendered response body, so no
    further response-model work is needed. A streamed response has been buffered
    into its body by `_drain_stream` before this runs.
    """
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
