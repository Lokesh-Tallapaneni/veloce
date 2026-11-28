"""Request handlers — `tools/call`, `resources/read`, `prompts/get` and log-level changes."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import veloce.status as status
from veloce._model_backend import shape_through_model
from veloce.contrib.mcp._helpers import (
    _binary_result,
    _declared_mime_type,
    _log_level_var,
    _normalize_prompt_messages,
    _principal_lacks_scopes,
    _progress_token,
    _resource_contents,
    _response_body_value,
    _RouteResponse,
    _ShortCircuit,
    _stringify,
    _text_result,
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
    _http_status_for,
    _tools_passing_filter,
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
from veloce.contrib.mcp.context import _LOG_RANKS, _session_var
from veloce.contrib.mcp.errors import (
    AuthorizationError,
    InternalError,
    InvalidParamsError,
    MCPError,
    ResourceNotFoundError,
    _ForbiddenError,
    _InBandError,
)
from veloce.http.response import Response
from veloce.principal import current_principal

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.prompts import PromptRegistry
    from veloce.contrib.mcp.registry import MCPTool, ToolFilter, ToolRegistry
    from veloce.contrib.mcp.resources import ResourceRegistry

_logger = logging.getLogger(__name__)


class CallsMixin:
    """The call-side handlers of `MCPServer`, mixed in so the server module stays readable."""

    # `MCPServer` is slotted, so a mixin without this regains a `__dict__`.
    __slots__ = ()

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes and members the host `MCPServer` and its other mixins provide.
        app: Any
        registry: ToolRegistry
        resources: ResourceRegistry
        prompts: PromptRegistry
        _tool_filter: ToolFilter | None
        _call_timeout: Any
        _result_dumps: Any

        async def _instrument(self, tool: MCPTool, started: float, status_code: int) -> None: ...
        async def _run_invoke(
            self,
            tool: MCPTool,
            arguments: dict[str, Any],
            progress_token: str | int | None,
            request_meta: dict[str, Any] | None = None,
        ) -> Any: ...
        async def _drain_stream(self, response: Response) -> None: ...
        def _success_result(self, tool: MCPTool, shaped: Any) -> dict[str, Any]: ...
        def _shape_result(self, tool: MCPTool, result: Any) -> Any: ...
        def _result_from_response(
            self, tool: MCPTool, response: Response, model_filtered: bool
        ) -> dict[str, Any]: ...
        def _create_task(
            self, tool: MCPTool, arguments: dict[str, Any], params: dict[str, Any]
        ) -> dict[str, Any]: ...

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

        # A tool this connection hid, or that a configured policy hides, is
        # rejected the same way an unregistered name would be - not a
        # permission error, since nothing distinguishes "hidden" from "never
        # registered" to the caller.
        session = _session_var.get()
        hidden = session.hidden if session is not None else None
        if hidden and tool.name in hidden:
            raise InvalidParamsError(f"Unknown tool: {name}")
        tool_filter = self._tool_filter
        if tool_filter is not None and not await _tools_passing_filter(
            tool_filter, [tool], current_principal()
        ):
            raise InvalidParamsError(f"Unknown tool: {name}")

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
        except Exception as exc:  # noqa: BLE001 - any tool failure becomes a protocol error result
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
            except Exception:  # noqa: BLE001 - a result that fails the output schema is reported as a tool error
                return _text_result(
                    "tool result does not conform to the declared output schema", is_error=True
                )
        return self._success_result(tool, shaped)

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
