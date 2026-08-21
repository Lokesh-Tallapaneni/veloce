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

Session management is opt-in (``sessions=True``): the server mints an
``Mcp-Session-Id`` on the ``initialize`` result, then requires it on every later
request (``400`` when missing, ``404`` once terminated) and accepts a ``DELETE`` to
terminate it. Each id owns a real `MCPSession`, so a stateful HTTP connection is a
first-class `MCPServer` connection - it records the client's advertised
capabilities, scopes the in-flight cancellation registry and task ownership to that
one client, and carries the connection's resource subscriptions, exactly as the
stdio loop's one session does. The id lifecycle lives in `HttpSessionStore` behind
the `Transport` contract, which reclaims an idle never-terminated session.

The stateless default keeps each POST independent, dispatched under a throwaway
per-request session: it isolates the in-flight registry (so a concurrent POST's
colliding JSON-RPC id cannot cancel this one) but is not a persistent connection,
so it advertises and serves no per-connection feature (subscriptions, lifecycle
gating).

Resumability is opt-in (``resumable=True``): each SSE event carrying a JSON-RPC
payload gets an id encoding its originating POST stream, and the events are kept in
a bounded `SSEEventStore`. A client whose connection drops reconnects with a
``GET`` carrying ``Last-Event-ID``; the server replays only that one stream's missed
events (never another stream's) and closes. With resumability off a ``GET`` stays
``405`` and no ids or history are kept.

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
import secrets
from collections.abc import Sequence
from itertools import count
from typing import TYPE_CHECKING, Any, cast

from veloce import status
from veloce.contrib.mcp.auth import PROTECTED_RESOURCE_METADATA_PATH, MCPAuth
from veloce.contrib.mcp.errors import (
    _JSONRPC_FORBIDDEN,
    _JSONRPC_INTERNAL_ERROR,
    MCPError,
    OriginNotAllowedError,
    ProtocolVersionError,
    SessionNotFoundError,
    SessionRequiredError,
    _error,
)
from veloce.contrib.mcp.server import _SERVED_VERSION_SET, MCPServer, _notifier_var
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.transports.event_store import SSEEventStore
from veloce.contrib.mcp.transports.session_store import HttpSessionStore
from veloce.http.response import JSONResponse, Response
from veloce.principal import Principal, current_principal, set_principal
from veloce.sse import EventSourceResponse, ServerSentEvent

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.request import Request

_logger = logging.getLogger(__name__)

# JSON-RPC 2.0 Sec. 5.1 parse error - a body that is not a single JSON object.
_JSONRPC_PARSE_ERROR = -32700

# Header carrying the session id when session management is enabled (MCP
# 2025-06-18 Streamable HTTP transport: the server assigns it on the initialize
# result and the client echoes it on every later request).
_SESSION_ID_HEADER = "Mcp-Session-Id"

# Reconnect hint (milliseconds) emitted as the SSE `retry` field before a stream
# closes, so a disconnected client knows how long to wait before reconnecting
# (MCP 2025-11-25 transport: "send an SSE event with a standard retry field
# before closing").
_SSE_RETRY_MS = 3000

# Header a resuming client sends to name the last event it received, so the
# server replays only what came after it (WHATWG SSE / MCP transport resumability).
_LAST_EVENT_ID_HEADER = "Last-Event-ID"

# Monotonic source of SSE event ids for the non-resumable priming event. The id
# makes the stream's first frame addressable; when resumability is off the id is
# process-local and not persisted (no replay window is kept).
_event_id = count(1)

# Bytes of entropy per resumable stream id. URL-safe base64 keeps the id within
# the SSE-id visible-ASCII range and free of the `.` sequence separator, so an
# event id splits unambiguously into (stream, sequence).
_STREAM_ID_ENTROPY_BYTES = 12

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
    sessions: bool = False,
    resumable: bool = False,
) -> None:
    """Mount the Streamable HTTP transport for `server` at `path` on `app`.

    When `auth` is given the endpoint becomes an OAuth 2.1 resource server: each
    request is authenticated before dispatch, and the RFC 9728 protected-resource
    metadata is served so a client can discover the authorization server.
    `allowed_origins` enables `Origin` validation (DNS-rebinding defense).
    `exclude_middleware` names app middleware the transport routes opt out of -
    typically an app-wide auth middleware the transport's own `auth` replaces.

    `sessions` opts into `Mcp-Session-Id` lifecycle: the server assigns a session
    id on the `initialize` result, requires it on every later request (HTTP 400 if
    missing, 404 once terminated), and accepts a `DELETE` to terminate it. The
    default keeps the stateless behavior with no per-request session bookkeeping.

    `resumable` opts into SSE resumability: each streamed event carrying a payload
    gets an id encoding its originating stream, the events are kept in a bounded
    `SSEEventStore`, and a `GET` carrying `Last-Event-ID` replays only that stream's
    missed events. The default keeps no event ids or history and answers a `GET`
    405.
    """
    # When sessions are on, a terminated / evicted id reclaims everything the
    # session owns: its subscription connection (so a closed connection receives
    # nothing further) and its tasks (so a never-settling task does not leak past
    # its owner).
    store = HttpSessionStore(on_evict=server.evict_session) if sessions else None
    event_store = SSEEventStore() if resumable else None

    async def mcp_endpoint(request: Request) -> Response:
        # The Streamable HTTP transport is one endpoint serving the spec's verbs:
        # POST carries a JSON-RPC message; DELETE terminates a session (only when
        # session management is on); GET resumes a dropped stream when resumability
        # is on (it carries Last-Event-ID), else there is no standalone
        # server-to-client stream this server keeps, so a GET is answered 405.
        if request.method == "DELETE":
            return _handle_delete(store, request)
        if request.method == "GET":
            return _handle_get(event_store, request, allowed_origins)
        return await _handle_http(server, request, auth, allowed_origins, store, event_store)

    app.add_route(
        path,
        mcp_endpoint,
        methods=["POST", "GET", "DELETE"],
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
    store: HttpSessionStore | None = None,
    event_store: SSEEventStore | None = None,
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
        return JSONResponse(
            _error(None, _JSONRPC_PARSE_ERROR, "Parse error"),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if not isinstance(message, dict):
        return JSONResponse(
            _error(None, _JSONRPC_PARSE_ERROR, "Parse error"),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # When session management is on, every request except `initialize` must echo a
    # live session id (HTTP 400 if missing, 404 once terminated); `initialize`
    # mints a new id returned on its response. Each id owns a real `MCPSession`, so
    # the connection is a first-class server connection (capabilities, in-flight
    # scope, subscriptions, lifecycle). The stateless default uses a fresh
    # per-request session instead: it carries no id and lives only for this POST,
    # which still isolates the in-flight registry so a colliding JSON-RPC id from a
    # concurrent POST cannot cancel this one.
    is_initialize = message.get("method") == "initialize"
    session_id: str | None = None
    session: MCPSession
    if store is not None:
        try:
            session_id, session = _bind_session(store, request, is_initialize)
        except MCPError as exc:
            return JSONResponse(exc.to_error(message.get("id")), status_code=exc.http_status)
    else:
        # A throwaway session for this POST only: it isolates the in-flight registry
        # (so a concurrent POST's colliding id cannot cancel this one) but is not a
        # persistent connection, so it advertises and serves no per-connection
        # feature (subscriptions, lifecycle gating).
        session = MCPSession(persistent=False)

    is_request = "id" in message and isinstance(message.get("method"), str)
    accepts_sse = "text/event-stream" in request.headers.get("accept", "")
    if is_request and accepts_sse:
        return _stream_response(
            server, message, current_principal(), session, session_id, event_store
        )

    response = await server.handle_message(message, session)
    if response is None:
        # A notification or a response carries no reply (JSON-RPC 2.0 Sec. 4.1).
        return _with_session(Response(status_code=status.HTTP_202_ACCEPTED), session_id)
    # An authorization failure (insufficient scope) is surfaced as an HTTP 403 with
    # an RFC 6750 scope challenge, not a 200 carrying a JSON-RPC error, so a client
    # can drive a step-up authorization flow. This applies to the JSON path only;
    # an SSE request (handled above) has already committed a 200 stream, so its
    # forbidden error is delivered in-band as the JSON-RPC error event.
    error = response.get("error")
    if isinstance(error, dict) and error.get("code") == _JSONRPC_FORBIDDEN:
        scopes = (error.get("data") or {}).get("requiredScopes") or []
        return _forbidden(response, scopes)
    return _with_session(JSONResponse(response), session_id)


def _bind_session(
    store: HttpSessionStore, request: Request, is_initialize: bool
) -> tuple[str, MCPSession]:
    """Resolve the `(id, MCPSession)` for a request under session management.

    `initialize` mints and returns a fresh id with its session (echoed on its
    response). Any other request must carry a live `Mcp-Session-Id`: a missing
    header raises `SessionRequiredError` (HTTP 400) and an unknown / terminated id
    raises `SessionNotFoundError` (HTTP 404).
    """
    if is_initialize:
        return store.create()
    session_id = request.headers.get("mcp-session-id")
    if not session_id:
        raise SessionRequiredError("missing Mcp-Session-Id header")
    session = store.resolve(session_id)
    if session is None:
        raise SessionNotFoundError("unknown or terminated session")
    return session_id, session


def _with_session(response: Response, session_id: str | None) -> Response:
    """Attach the `Mcp-Session-Id` header to `response` when a session is bound."""
    if session_id is not None:
        response.headers[_SESSION_ID_HEADER] = session_id
    return response


def _handle_delete(store: HttpSessionStore | None, request: Request) -> Response:
    """Terminate the session named by `Mcp-Session-Id` (HTTP 204), or 404/405.

    A `DELETE` is meaningful only under session management: without it the verb is
    unsupported (HTTP 405). With it, a live id is terminated (HTTP 204) and a
    missing / already-terminated id is HTTP 404.
    """
    if store is None:
        return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})
    session_id = request.headers.get("mcp-session-id")
    if not session_id or not store.terminate(session_id):
        return JSONResponse(
            SessionNotFoundError("unknown or terminated session").to_error(None),
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _handle_get(
    event_store: SSEEventStore | None,
    request: Request,
    allowed_origins: frozenset[str] | None = None,
) -> Response:
    """Resume a dropped SSE stream from `Last-Event-ID`, or answer 405.

    A `GET` is meaningful only under resumability and only as a resume: the client
    names the last event it saw via `Last-Event-ID`, and the server replays the
    events that one stream produced after it (scoped to that stream, never another
    POST's). Without resumability, or without the header, there is no standalone
    server-push stream, so the verb is unsupported (HTTP 405).
    """
    # The Origin and protocol-version checks are the same spec MUSTs the POST path
    # runs; a resume must not bypass the DNS-rebinding defense, so they precede any
    # replay. A violation raises an `MCPError` subclass carrying its HTTP status.
    try:
        _validate_origin(request, allowed_origins)
        _validate_protocol_version(request)
    except MCPError as exc:
        return JSONResponse(exc.to_error(None), status_code=exc.http_status)

    if event_store is None:
        return _method_not_allowed()
    last_event_id = request.headers.get(_LAST_EVENT_ID_HEADER)
    if not last_event_id:
        return _method_not_allowed()
    missed = event_store.replay_after(last_event_id)

    async def events() -> Any:
        for event_id, payload in missed:
            yield ServerSentEvent.json(payload, id=event_id)
        # Close the resumed stream with the same reconnect hint a live stream uses,
        # so a second drop reconnects on the same schedule.
        yield ServerSentEvent(retry=_SSE_RETRY_MS)

    return EventSourceResponse(events())


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
    if version is not None and version not in _SERVED_VERSION_SET:
        raise ProtocolVersionError(f"unsupported MCP-Protocol-Version: {version}")


def _method_not_allowed() -> Response:
    """Answer a GET on the MCP endpoint with HTTP 405 (no server-push stream)."""
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})


def _forbidden(response: dict[str, Any], scopes: list[str]) -> Response:
    """Build an HTTP 403 with a `WWW-Authenticate` insufficient-scope challenge."""
    parts = ['Bearer error="insufficient_scope"']
    if scopes:
        parts.append(f'scope="{" ".join(scopes)}"')
    return JSONResponse(
        response,
        status_code=status.HTTP_403_FORBIDDEN,
        headers={"WWW-Authenticate": ", ".join(parts)},
    )


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
    server: MCPServer,
    message: dict[str, Any],
    principal: Principal | None,
    session: MCPSession,
    session_id: str | None = None,
    event_store: SSEEventStore | None = None,
) -> EventSourceResponse:
    """Answer one request as an SSE stream: its notifications then its response.

    The request is dispatched in a background task whose context carries an
    outbound sink wired to a queue; the SSE generator drains that queue, emitting
    each progress / log notification as it is produced and finally the JSON-RPC
    response. Per-request `_notifier_var` scoping keeps concurrent HTTP calls from
    crossing notifications.

    For the open lifetime of the stream the connection is registered with the
    server keyed by its `session`, with this stream's sink as its outbound channel,
    so an application-signalled `notifications/resources/updated` reaches a client
    that has `resources/subscribe`d (a no-op when subscriptions are off). The
    connection is unregistered when the stream closes.

    The stream opens with a priming event (an id and an empty `data` field) so the
    first frame is addressable, and closes with a `retry` field hinting the
    reconnect delay (MCP 2025-11-25 transport). The dispatch task is left running
    on client disconnection rather than cancelled - the spec forbids treating a
    dropped connection as the client cancelling its request.

    When `event_store` is given (resumability is on) each payload event carries an
    id encoding this stream and is recorded before being queued, so a client that
    drops can replay the tail via a `Last-Event-ID` GET. The runner records into
    the store regardless of the generator's state, so events produced after a
    disconnect are still available to replay.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()
    # A resumable stream gets a unique id so its event ids encode their origin and
    # a resume replays only this stream's events. `seq` advances per recorded
    # payload; 0 is reserved for the priming event so the replay base is addressable.
    stream_id = secrets.token_urlsafe(_STREAM_ID_ENTROPY_BYTES) if event_store is not None else None
    seq = count(1)

    def emit_id(payload: dict[str, Any]) -> str | None:
        # Record the payload and return its event id when resumability is on; the
        # stateless default records nothing and the event carries no id.
        if event_store is None or stream_id is None:
            return None
        return event_store.record(stream_id, next(seq), payload)

    # This sink is the HTTP transport's `Transport.send`: it records the outbound
    # JSON-RPC message (when resumable) then queues it for the SSE generator. The
    # recording is what survives a dropped connection for later replay.
    async def send(message: dict[str, Any]) -> None:
        await queue.put((emit_id(message), message))

    async def runner() -> None:
        token = _notifier_var.set(send)
        # Register this connection so an application-signalled resource update fans
        # out to it while the stream is open (a no-op when subscriptions are off);
        # unregistered in `finally` so a closed stream receives nothing further.
        conn_token = server.register_connection(session, send)
        # The runner task inherits the request's context (and its principal), but
        # re-bind explicitly so identity is correct regardless of how the task was
        # scheduled.
        set_principal(principal)
        try:
            response = await server.handle_message(message, session)
            if response is not None:
                await queue.put((emit_id(response), response))
        except asyncio.CancelledError:
            # The client cancelled this request (notifications/cancelled): the
            # call's task was cancelled deliberately. Close the stream cleanly
            # without an error frame - a cancelled request expects no response.
            pass
        except Exception:
            _logger.exception("MCP HTTP request handling failed")
            err = _error(message.get("id"), _JSONRPC_INTERNAL_ERROR, "internal error")
            await queue.put((emit_id(err), err))
        finally:
            server.unregister_connection(conn_token)
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
        # gives the first frame an id, before any notification or the response. A
        # resumable stream's priming id encodes the stream (sequence 0) so a resume
        # from it replays the whole tail.
        prime_id = f"{stream_id}.0" if stream_id is not None else str(next(_event_id))
        yield ServerSentEvent(data="", id=prime_id)
        while True:
            item = await queue.get()
            if item is _STREAM_END:
                break
            event_id, payload = item
            yield ServerSentEvent.json(payload, id=event_id)
        # A closing frame carrying the reconnect hint, so a client that drops
        # mid-stream knows how long to wait before reconnecting.
        yield ServerSentEvent(retry=_SSE_RETRY_MS)
        await runner_task

    headers = {_SESSION_ID_HEADER: session_id} if session_id is not None else None
    return EventSourceResponse(events(), headers=headers)
