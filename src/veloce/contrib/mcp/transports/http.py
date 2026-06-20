"""Streamable HTTP transport — the MCP remote transport on a mounted route.

A single endpoint serves both verbs the spec mandates: a ``POST`` accepts one
JSON-RPC message and replies through the same transport-agnostic
`MCPServer.handle_message` the stdio transport uses, so tools, resources, and
prompts behave identically over HTTP; a ``GET`` (which would open a standalone
server-to-client stream this server does not keep) is answered ``405``. When the
client's ``Accept`` header offers ``text/event-stream`` the POST reply is an SSE
stream that carries the call's progress / log notifications followed by the
JSON-RPC response; otherwise a single JSON response is returned. A message that
needs no reply (a notification or a response) is answered with ``202 Accepted``.

Before dispatch the transport enforces two spec MUSTs: a present ``Origin`` outside
the configured allowlist is rejected ``403`` (DNS-rebinding defense; a missing
``Origin`` is allowed), and a present ``MCP-Protocol-Version`` header naming an
unsupported revision is rejected ``400`` (a missing header keeps the negotiated
version). Each violation raises an `MCPError` subclass carrying its HTTP status.

The endpoint is an ordinary Veloce route, so it is protected by whatever middleware
and dependencies the app applies to it (an OAuth Resource-Server check, an API-key
scheme) - the transport adds no auth of its own.

This transport satisfies the `Transport` contract through its per-request outbound
sink (`send` in `_stream_response`): the SSE generator drains a queue the sink
feeds, so one-way notifications reach the client while the call is in flight.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from itertools import count
from typing import TYPE_CHECKING, Any, cast

from veloce.contrib.mcp.auth import PROTECTED_RESOURCE_METADATA_PATH, MCPAuth
from veloce.contrib.mcp.errors import (
    _JSONRPC_FORBIDDEN,
    _JSONRPC_INTERNAL_ERROR,
    MCPError,
    OriginNotAllowedError,
    ProtocolVersionError,
    _error,
)
from veloce.contrib.mcp.server import _SUPPORTED_PROTOCOL_VERSIONS, MCPServer, _notifier_var
from veloce.http.response import JSONResponse, Response
from veloce.principal import Principal, current_principal, set_principal
from veloce.sse import EventSourceResponse, ServerSentEvent

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.request import Request

_logger = logging.getLogger(__name__)

# JSON-RPC 2.0 Sec. 5.1 parse error - a body that is not a single JSON object.
_JSONRPC_PARSE_ERROR = -32700

# Reconnect hint (milliseconds) emitted as the SSE `retry` field before a stream
# closes, so a disconnected client knows how long to wait before reconnecting
# (MCP 2025-11-25 transport: "send an SSE event with a standard retry field
# before closing").
_SSE_RETRY_MS = 3000

# Monotonic source of SSE event ids for the priming event. The id makes the
# stream's first frame addressable; a real resumable event store is a later
# feature, so the id is process-local and not persisted.
_event_id = count(1)

# Sentinel marking the end of an SSE response stream (the runner has produced the
# call's notifications and final response).
_STREAM_END = object()


def register_http_transport(
    app: Any,
    server: MCPServer,
    path: str = "/mcp",
    auth: MCPAuth | None = None,
    allowed_origins: frozenset[str] | None = None,
    exclude_middleware: Sequence[str] | None = None,
) -> None:
    """Mount the Streamable HTTP transport for `server` at `path` on `app`.

    When `auth` is given the endpoint becomes an OAuth 2.1 resource server: each
    request is authenticated before dispatch, and the RFC 9728 protected-resource
    metadata is served so a client can discover the authorization server.
    `allowed_origins` enables `Origin` validation (DNS-rebinding defense).
    `exclude_middleware` names app middleware the transport routes opt out of -
    typically an app-wide auth middleware the transport's own `auth` replaces.
    """

    async def mcp_endpoint(request: Request) -> Response:
        # The Streamable HTTP transport is one endpoint serving both verbs: POST
        # carries a JSON-RPC message; GET would open a standalone server-to-client
        # SSE stream. This server keeps no such stream, so a GET is answered 405.
        if request.method == "GET":
            return _method_not_allowed()
        return await _handle_http(server, request, auth, allowed_origins)

    app.add_route(
        path,
        mcp_endpoint,
        methods=["POST", "GET"],
        include_in_schema=False,
        exclude_middleware=exclude_middleware,
    )

    if auth is not None:

        async def mcp_metadata(request: Request) -> Response:
            return JSONResponse(auth.metadata())

        app.add_route(
            PROTECTED_RESOURCE_METADATA_PATH,
            mcp_metadata,
            methods=["GET"],
            include_in_schema=False,
            exclude_middleware=exclude_middleware,
        )


async def _handle_http(
    server: MCPServer,
    request: Request,
    auth: MCPAuth | None,
    allowed_origins: frozenset[str] | None = None,
) -> Response:
    """Authenticate, then dispatch one JSON-RPC message from an HTTP POST body."""
    # Origin and protocol-version checks are spec MUSTs that precede dispatch; a
    # violation raises an `MCPError` subclass carrying its own HTTP status, so the
    # two checks share one rejection path (a JSON-RPC error body at that status).
    try:
        _validate_origin(request, allowed_origins)
        _validate_protocol_version(request)
    except MCPError as exc:
        return JSONResponse(exc.to_error(None), status_code=exc.http_status)

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
        return _stream_response(server, message, current_principal())

    response = await server.handle_message(message)
    if response is None:
        # A notification or a response carries no reply (JSON-RPC 2.0 Sec. 4.1).
        return Response(status_code=202)
    # An authorization failure (insufficient scope) is surfaced as an HTTP 403 with
    # an RFC 6750 scope challenge, not a 200 carrying a JSON-RPC error, so a client
    # can drive a step-up authorization flow. This applies to the JSON path only;
    # an SSE request (handled above) has already committed a 200 stream, so its
    # forbidden error is delivered in-band as the JSON-RPC error event.
    error = response.get("error")
    if isinstance(error, dict) and error.get("code") == _JSONRPC_FORBIDDEN:
        scopes = (error.get("data") or {}).get("requiredScopes") or []
        return _forbidden(response, scopes)
    return JSONResponse(response)


def _validate_origin(request: Request, allowed_origins: frozenset[str] | None) -> None:
    """Reject a present `Origin` outside the allowlist (DNS-rebinding defense).

    A missing `Origin` (a non-browser client) is allowed; a browser-set `Origin`
    not in `allowed_origins` raises `OriginNotAllowedError` (HTTP 403). Validation
    is skipped entirely when no allowlist is configured.
    """
    if allowed_origins is None:
        return
    origin = request.headers.get("origin")
    if origin is not None and origin not in allowed_origins:
        raise OriginNotAllowedError("origin not allowed")


def _validate_protocol_version(request: Request) -> None:
    """Reject an unsupported `MCP-Protocol-Version` header (HTTP 400).

    The header binds a post-initialization request to a negotiated revision. A
    missing header keeps the legacy behavior (the version negotiated at
    `initialize` stands); a present value this server does not speak raises
    `ProtocolVersionError`.
    """
    version = request.headers.get("mcp-protocol-version")
    if version is not None and version not in _SUPPORTED_PROTOCOL_VERSIONS:
        raise ProtocolVersionError(f"unsupported MCP-Protocol-Version: {version}")


def _method_not_allowed() -> Response:
    """Answer a GET on the MCP endpoint with HTTP 405 (no server-push stream)."""
    return Response(status_code=405, headers={"Allow": "POST"})


def _forbidden(response: dict[str, Any], scopes: list[str]) -> Response:
    """Build an HTTP 403 with a `WWW-Authenticate` insufficient-scope challenge."""
    parts = ['Bearer error="insufficient_scope"']
    if scopes:
        parts.append(f'scope="{" ".join(scopes)}"')
    return JSONResponse(response, status_code=403, headers={"WWW-Authenticate": ", ".join(parts)})


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


def _stream_response(
    server: MCPServer, message: dict[str, Any], principal: Principal | None
) -> EventSourceResponse:
    """Answer one request as an SSE stream: its notifications then its response.

    The request is dispatched in a background task whose context carries an
    outbound sink wired to a queue; the SSE generator drains that queue, emitting
    each progress / log notification as it is produced and finally the JSON-RPC
    response. Per-request `_notifier_var` scoping keeps concurrent HTTP calls from
    crossing notifications.

    The stream opens with a priming event (an id and an empty `data` field) so the
    first frame is addressable, and closes with a `retry` field hinting the
    reconnect delay (MCP 2025-11-25 transport). The dispatch task is left running
    on client disconnection rather than cancelled - the spec forbids treating a
    dropped connection as the client cancelling its request.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()

    # This sink is the HTTP transport's `Transport.send`: one outbound JSON-RPC
    # message onto the queue the SSE generator drains.
    async def send(message: dict[str, Any]) -> None:
        await queue.put(message)

    async def runner() -> None:
        token = _notifier_var.set(send)
        # The runner task inherits the request's context (and its principal), but
        # re-bind explicitly so identity is correct regardless of how the task was
        # scheduled.
        set_principal(principal)
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
        # Disconnection is not cancellation (MCP transport): a client that closes
        # the stream must not abort the in-flight call, so the dispatch task is
        # never cancelled on generator teardown. It holds its own context and
        # finishes on its own; the `runner_task` binding is held in this frame for
        # the generator's lifetime, keeping the task from being garbage-collected
        # mid-flight, and the queue handshake guarantees the generator outlives it.
        runner_task = asyncio.ensure_future(runner())
        # Priming event: an id plus an empty data field marks the stream open and
        # gives the first frame an id, before any notification or the response.
        yield ServerSentEvent(data="", id=str(next(_event_id)))
        while True:
            item = await queue.get()
            if item is _STREAM_END:
                break
            yield ServerSentEvent.json(item)
        # A closing frame carrying the reconnect hint, so a client that drops
        # mid-stream knows how long to wait before reconnecting.
        yield ServerSentEvent(retry=_SSE_RETRY_MS)
        await runner_task

    return EventSourceResponse(events())
