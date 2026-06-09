"""Streamable HTTP transport - the MCP remote transport on a mounted route.

A single ``POST`` endpoint accepts one JSON-RPC message and replies through the
same transport-agnostic `MCPServer.handle_message` the stdio transport uses, so
tools, resources, and prompts behave identically over HTTP. When the client's
``Accept`` header offers ``text/event-stream`` the reply is an SSE stream that
carries the call's progress / log notifications followed by the JSON-RPC response;
otherwise a single JSON response is returned. A message that needs no reply (a
notification or a response) is answered with ``202 Accepted``.

The endpoint is an ordinary Veloce route, so it is protected by whatever middleware
and dependencies the app applies to it (an OAuth Resource-Server check, an API-key
scheme) - the transport adds no auth of its own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from veloce.contrib.mcp.auth import PROTECTED_RESOURCE_METADATA_PATH, MCPAuth
from veloce.contrib.mcp.server import (
    _JSONRPC_INTERNAL_ERROR,
    MCPServer,
    _error,
    _notifier_var,
)
from veloce.http.response import JSONResponse, Response
from veloce.principal import Principal, set_principal
from veloce.sse import EventSourceResponse, ServerSentEvent

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.request import Request

_logger = logging.getLogger(__name__)

# JSON-RPC 2.0 Sec. 5.1 parse error - a body that is not a single JSON object.
_JSONRPC_PARSE_ERROR = -32700

# Sentinel marking the end of an SSE response stream (the runner has produced the
# call's notifications and final response).
_STREAM_END = object()


def register_http_transport(
    app: Any, server: MCPServer, path: str = "/mcp", auth: MCPAuth | None = None
) -> None:
    """Mount the Streamable HTTP transport for `server` at `path` on `app`.

    When `auth` is given the endpoint becomes an OAuth 2.1 resource server: each
    request is authenticated before dispatch, and the RFC 9728 protected-resource
    metadata is served so a client can discover the authorization server.
    """

    async def mcp_endpoint(request: Request) -> Response:
        return await _handle_http(server, request, auth)

    app.add_route(path, mcp_endpoint, methods=["POST"], include_in_schema=False)

    if auth is not None:

        async def mcp_metadata(request: Request) -> Response:
            return JSONResponse(auth.metadata())

        app.add_route(
            PROTECTED_RESOURCE_METADATA_PATH,
            mcp_metadata,
            methods=["GET"],
            include_in_schema=False,
        )


async def _handle_http(server: MCPServer, request: Request, auth: MCPAuth | None) -> Response:
    """Authenticate, then dispatch one JSON-RPC message from an HTTP POST body."""
    if auth is not None:
        principal, challenge = await _authenticate(auth, request)
        if challenge is not None:
            return challenge
        # Publish the identity for the duration of this request so the dispatched
        # tool / resource (and any business dependency) reads it through
        # `current_principal`; the SSE runner task copies this context.
        set_principal(principal)

    try:
        message = await request.json()
    except Exception:
        return JSONResponse(_error(None, _JSONRPC_PARSE_ERROR, "Parse error"), status_code=400)
    if not isinstance(message, dict):
        return JSONResponse(_error(None, _JSONRPC_PARSE_ERROR, "Parse error"), status_code=400)

    is_request = "id" in message and isinstance(message.get("method"), str)
    accepts_sse = "text/event-stream" in request.headers.get("accept", "")
    if is_request and accepts_sse:
        return _stream_response(server, message)

    response = await server.handle_message(message)
    if response is None:
        # A notification or a response carries no reply (JSON-RPC 2.0 Sec. 4.1).
        return Response(status_code=202)
    return JSONResponse(response)


async def _authenticate(
    auth: MCPAuth, request: Request
) -> tuple[Principal | None, Response | None]:
    """Validate the request's bearer token; return `(principal, challenge)`.

    A missing or invalid token yields a `401` challenge; a valid token missing the
    endpoint's required scopes yields a `403`. On success the challenge is `None`.
    """
    header = request.headers.get("authorization", "")
    scheme, _, raw_token = header.partition(" ")
    token = raw_token.strip()
    if scheme.lower() != "bearer" or not token:
        return None, _challenge(auth, 401, "invalid_token")

    try:
        outcome = auth.verify(token)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        principal = cast("Principal | None", outcome)
    except Exception:
        _logger.exception("MCP token verification raised")
        principal = None
    if principal is None:
        return None, _challenge(auth, 401, "invalid_token")

    if auth.required_scopes and not principal.has_scopes(auth.required_scopes):
        return None, _challenge(auth, 403, "insufficient_scope")
    return principal, None


def _challenge(auth: MCPAuth, status_code: int, error: str) -> Response:
    """Build a `401`/`403` response with the RFC 6750 `WWW-Authenticate` challenge."""
    parts = [f'Bearer error="{error}"', f'resource_metadata="{PROTECTED_RESOURCE_METADATA_PATH}"']
    if error == "insufficient_scope" and auth.required_scopes:
        parts.append(f'scope="{" ".join(sorted(auth.required_scopes))}"')
    body = {"error": error}
    return JSONResponse(
        body, status_code=status_code, headers={"WWW-Authenticate": ", ".join(parts)}
    )


def _stream_response(server: MCPServer, message: dict[str, Any]) -> EventSourceResponse:
    """Answer one request as an SSE stream: its notifications then its response.

    The request is dispatched in a background task whose context carries an
    outbound sink wired to a queue; the SSE generator drains that queue, emitting
    each progress / log notification as it is produced and finally the JSON-RPC
    response. Per-request `_notifier_var` scoping keeps concurrent HTTP calls from
    crossing notifications.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def sink(notification: dict[str, Any]) -> None:
        await queue.put(notification)

    async def runner() -> None:
        token = _notifier_var.set(sink)
        try:
            response = await server.handle_message(message)
            if response is not None:
                await queue.put(response)
        except Exception:
            _logger.exception("MCP HTTP request handling failed")
            await queue.put(_error(message.get("id"), _JSONRPC_INTERNAL_ERROR, "internal error"))
        finally:
            _notifier_var.reset(token)
            await queue.put(_STREAM_END)

    async def events() -> Any:
        task = asyncio.ensure_future(runner())
        try:
            while True:
                item = await queue.get()
                if item is _STREAM_END:
                    break
                yield ServerSentEvent.json(item)
        finally:
            if not task.done():
                task.cancel()

    return EventSourceResponse(events())
