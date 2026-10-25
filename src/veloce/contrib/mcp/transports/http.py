"""Streamable HTTP transport — the MCP remote transport on a mounted route.

A single endpoint serves both verbs the spec mandates. A ``POST`` accepts one
JSON-RPC message and replies through the same transport-agnostic
`MCPServer.handle_message` the stdio transport uses, so tools, resources and
prompts behave identically over HTTP; a ``GET`` is answered ``405`` unless it
resumes a dropped stream. When the client's ``Accept`` header offers
``text/event-stream`` the POST reply is an SSE stream carrying the call's progress
/ log notifications followed by the JSON-RPC response; otherwise a single JSON
response is returned. A message needing no reply is answered ``202 Accepted``.

Before dispatch the transport enforces three spec MUSTs: a present ``Origin``
outside the configured allowlist is rejected ``403`` (DNS-rebinding defense; a
missing ``Origin`` is allowed), a present ``MCP-Protocol-Version`` naming an
unsupported revision is rejected ``400`` (a missing header keeps the negotiated
version), and on the modern revision the standard request headers are required and
cross-checked against the body they label (``400``, JSON-RPC ``-32020``).

Session management is opt-in (``sessions=True``): the server mints an
``Mcp-Session-Id`` on the ``initialize`` result, then requires it on every later
request (``400`` when missing, ``404`` once terminated) and accepts a ``DELETE`` to
terminate it. Each id owns a real `MCPSession`, so a stateful HTTP connection is a
first-class `MCPServer` connection - scoping the in-flight cancellation registry
and task ownership to that one client, and carrying its resource subscriptions,
exactly as the stdio loop's one session does. The stateless default keeps each POST
independent under a throwaway session: the registry is still isolated, but nothing
per-connection (subscriptions, lifecycle gating) is advertised or served.

Resumability is opt-in (``resumable=True``): each SSE event carrying a JSON-RPC
payload gets an id encoding its originating POST stream, and the events are kept in
a bounded `SSEEventStore`. A client whose connection drops reconnects with a
``GET`` carrying ``Last-Event-ID``; the server replays only that one stream's
missed events, never another stream's, and closes.

The endpoint is an ordinary Veloce route, so it is protected by whatever middleware
and dependencies the app applies to it - the transport adds no auth of its own.

This module owns the admission and response helpers both HTTP wires use, so the two
cannot drift apart: `sse.py` imports `_protocol_response`, `_validate_origin`,
`_authenticate`, `register_metadata_route` and `_SSE_RETRY_MS` from here.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from collections.abc import AsyncIterator, Sequence
from itertools import count
from typing import TYPE_CHECKING, Any

import veloce.status as status
from veloce.contrib.mcp._helpers import encode_envelope, transport_route_name
from veloce.contrib.mcp._posture import record_endpoint
from veloce.contrib.mcp.auth import MCPAuth
from veloce.contrib.mcp.context import _transport_var
from veloce.contrib.mcp.errors import (
    _JSONRPC_FORBIDDEN,
    _JSONRPC_INTERNAL_ERROR,
    HeaderMismatchError,
    MCPError,
    ProtocolVersionError,
    SessionNotFoundError,
    SessionRequiredError,
    _error,
    invalid_request_error,
    parse_error,
)
from veloce.contrib.mcp.server import (
    _SERVED_VERSION_SET,
    META_PROTOCOL_VERSION,
    MCPServer,
    _notifier_var,
    _requested_version,
    is_modern_version,
)
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.transports._common import (
    _SSE_RETRY_MS,
    _authenticate,
    _protocol_response,
    _validate_origin,
    register_metadata_route,
)
from veloce.contrib.mcp.transports.event_store import _EVENT_ID_SEP, SSEEventStore
from veloce.contrib.mcp.transports.session_store import HttpSessionStore, SessionBackend
from veloce.http.response import Response
from veloce.principal import Principal, current_principal, set_principal
from veloce.sse import EventSourceResponse, ServerSentEvent

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.request import Request

_logger = logging.getLogger(__name__)

# Assigned on the `initialize` result, echoed on every later request (MCP 2025-06-18).
_SESSION_ID_HEADER = "Mcp-Session-Id"

# Names the last event received, so only what came after it is replayed (WHATWG SSE).
_LAST_EVENT_ID_HEADER = "Last-Event-ID"
_RAW_LAST_EVENT_ID = _LAST_EVENT_ID_HEADER.lower().encode("latin-1")

# Ids for the non-resumable priming event: process-local, no replay window kept.
_event_id = count(1)

# URL-safe base64 keeps the id free of the `.` that splits (stream, sequence).
_STREAM_ID_ENTROPY_BYTES = 12

_STREAM_END = object()


# ── Route registration ────────────────────────────────────


def register_http_transport(
    app: Any,
    server: MCPServer,
    path: str = "/mcp",
    auth: MCPAuth | None = None,
    allowed_origins: frozenset[str] | None = None,
    exclude_middleware: Sequence[str] | None = None,
    sessions: bool = False,
    resumable: bool = False,
    session_backend: SessionBackend | None = None,
) -> None:
    """Mount the Streamable HTTP transport for `server` at `path` on `app`.

    When `auth` is given the endpoint becomes an OAuth 2.1 resource server: each
    request is authenticated before dispatch, and the RFC 9728 protected-resource
    metadata is served so a client can discover the authorization server.
    `allowed_origins` enables `Origin` validation (DNS-rebinding defense).
    `exclude_middleware` names app middleware the transport routes opt out of -
    typically an app-wide auth middleware the transport's own `auth` replaces.

    `sessions` opts into the `Mcp-Session-Id` lifecycle; the default keeps the
    stateless behavior with no per-request session bookkeeping. `session_backend`
    shares those sessions between workers: without one a session lives in the
    process that minted it, so a request reaching another worker is answered 404.

    `resumable` opts into SSE resumability, so a `GET` carrying `Last-Event-ID`
    replays that stream's missed events. The default keeps no ids or history.
    """
    store = (
        HttpSessionStore(on_evict=server.evict_session, backend=session_backend)
        if sessions
        else None
    )
    event_store = SSEEventStore() if resumable else None

    async def mcp_endpoint(request: Request) -> Response:
        # Above the verb switch: a check inside a handler is one a new verb can omit.
        try:
            _validate_origin(request, allowed_origins)
            _validate_protocol_version(request)
        except MCPError as exc:
            return _protocol_response(exc.to_error(None), status_code=exc.http_status)

        # On the request's own context, which the streaming runner inherits.
        _transport_var.set("http")

        if auth is not None:
            principal, challenge = await _authenticate(auth, request)
            if challenge is not None:
                return challenge
            set_principal(principal)

        if request.method == "DELETE":
            return await _handle_delete(store, request)
        if request.method == "GET":
            return await _handle_get(event_store, request, store)
        return await _handle_http(server, request, store, event_store)

    app.add_route(
        path,
        mcp_endpoint,
        methods=["POST", "GET", "DELETE"],
        include_in_schema=False,
        exclude_middleware=exclude_middleware,
        name=transport_route_name("mcp_endpoint", path),
    )
    record_endpoint(app, "http", path, auth, allowed_origins)

    register_metadata_route(app, auth, exclude_middleware)


async def _handle_http(
    server: MCPServer,
    request: Request,
    store: HttpSessionStore | None = None,
    event_store: SSEEventStore | None = None,
) -> Response:
    """Dispatch one JSON-RPC message from an HTTP POST body.

    The caller has already run admission control (`Origin`, `MCP-Protocol-Version`,
    authentication) and published the principal.
    """
    try:
        message = await request.json()
    except Exception:
        return _protocol_response(parse_error(), status_code=status.HTTP_400_BAD_REQUEST)
    if not request.data:
        return _protocol_response(parse_error(), status_code=status.HTTP_400_BAD_REQUEST)
    if not isinstance(message, dict):
        # JSON-RPC: -32700 is unreadable text, -32600 is not a Request object.
        return _protocol_response(invalid_request_error(), status_code=status.HTTP_400_BAD_REQUEST)

    # The headers label a body a proxy forwarded without parsing it.
    try:
        _validate_standard_headers(request, message)
    except MCPError as exc:
        return _protocol_response(exc.to_error(message.get("id")), status_code=exc.http_status)

    is_initialize = message.get("method") == "initialize"
    session_id: str | None = None
    session: MCPSession
    if store is not None:
        try:
            session_id, session = await _bind_session(store, request, is_initialize)
        except MCPError as exc:
            return _protocol_response(exc.to_error(message.get("id")), status_code=exc.http_status)
    else:
        session = MCPSession(persistent=False)

    is_request = "id" in message and isinstance(message.get("method"), str)
    # The real parser: a substring test reads `text/event-streaming` as a match.
    accepts_sse = request.accept_mimetypes.quality_explicit("text/event-stream") > 0
    # In the branch, so a client offering the stream but not JSON keeps the stream.
    if (
        is_request
        and accepts_sse
        and (
            request.accept_mimetypes.quality("application/json") <= 0
            or _needs_stream(server, message, event_store)
        )
    ):
        return _stream_response(
            server, message, current_principal(), session, session_id, event_store, store
        )

    response = await server.handle_message(message, session)
    # So a later request landing on another worker sees it; no-op without a backend.
    if store is not None and session_id is not None:
        await store.persist(session_id, session)
    if response is None:
        # A notification or a response carries no reply (JSON-RPC 2.0 Sec. 4.1).
        return _with_session(Response(status_code=status.HTTP_202_ACCEPTED), session_id)
    # A 403 with an RFC 6750 challenge; an SSE reply has committed its 200.
    error = response.get("error")
    if isinstance(error, dict) and error.get("code") == _JSONRPC_FORBIDDEN:
        scopes = (error.get("data") or {}).get("requiredScopes") or []
        return _forbidden(response, scopes)
    return _with_session(_protocol_response(response), session_id)


async def _bind_session(
    store: HttpSessionStore, request: Request, is_initialize: bool
) -> tuple[str, MCPSession]:
    """Resolve the `(id, MCPSession)` for a request under session management.

    `initialize` mints and returns a fresh id with its session (echoed on its
    response). Any other request must carry a live `Mcp-Session-Id`: a missing
    header raises `SessionRequiredError` (HTTP 400) and an unknown / terminated id
    raises `SessionNotFoundError` (HTTP 404).
    """
    if is_initialize:
        return await store.create()
    session_id = request._peek_header_key(b"mcp-session-id")
    if not session_id:
        raise SessionRequiredError("missing Mcp-Session-Id header")
    session = await store.resolve(session_id)
    if session is None:
        raise SessionNotFoundError("unknown or terminated session")
    return session_id, session


def _with_session(response: Response, session_id: str | None) -> Response:
    """Attach the `Mcp-Session-Id` header to `response` when a session is bound."""
    if session_id is not None:
        response.headers[_SESSION_ID_HEADER] = session_id
    return response


async def _handle_delete(store: HttpSessionStore | None, request: Request) -> Response:
    """Terminate the session named by `Mcp-Session-Id` (HTTP 204), or 404/405.

    A `DELETE` is meaningful only under session management: without it the verb is
    unsupported (HTTP 405).

    The caller has already run admission control, so terminating a session requires
    the same credential and passes the same `Origin` check as any other request.
    """
    if store is None:
        return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})
    session_id = request._peek_header_key(b"mcp-session-id")
    if not session_id or not await store.terminate(session_id):
        return _protocol_response(
            SessionNotFoundError("unknown or terminated session").to_error(None),
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Verb handlers ─────────────────────────────────────────


async def _handle_get(
    event_store: SSEEventStore | None, request: Request, store: HttpSessionStore | None
) -> Response:
    """Resume a dropped SSE stream from `Last-Event-ID`, or answer 405.

    The replay is scoped to the stream the client names, never another POST's.
    Without resumability, or without the header, there is no standalone server-push
    stream, so the verb is unsupported (HTTP 405).

    Under session management the id is checked first, through the same
    `_bind_session` gate a `POST` goes through. `SSEEventStore` has no session
    dimension, so without that a client whose session had been `DELETE`d or evicted
    could still replay that stream's buffered payloads, tool results included.

    The caller has already run admission control, so a replay cannot bypass the
    DNS-rebinding defense or reach an unauthenticated client.
    """
    if event_store is None:
        return _method_not_allowed()
    last_event_id = request._peek_header_key(_RAW_LAST_EVENT_ID)
    if not last_event_id:
        return _method_not_allowed()
    if store is not None:
        try:
            await _bind_session(store, request, is_initialize=False)
        except MCPError as exc:
            return _protocol_response(exc.to_error(None), status_code=exc.http_status)
    missed = event_store.replay_after(last_event_id)

    async def events() -> AsyncIterator[ServerSentEvent]:
        for event_id, payload in missed:
            yield ServerSentEvent(data=encode_envelope(payload).decode(), id=event_id)
        yield ServerSentEvent(retry=_SSE_RETRY_MS)

    return EventSourceResponse(events())


# ── Admission control ─────────────────────────────────────


def _validate_protocol_version(request: Request) -> None:
    """Reject an unsupported `MCP-Protocol-Version` header (HTTP 400).

    The header binds a post-initialization request to a negotiated revision. A
    missing header keeps the legacy behavior (the version negotiated at
    `initialize` stands); a present value this server does not speak raises
    `ProtocolVersionError`.
    """
    version = request._peek_header_key(b"mcp-protocol-version")
    if version is not None and version not in _SERVED_VERSION_SET:
        raise ProtocolVersionError(f"unsupported MCP-Protocol-Version: {version}")


# The methods whose `Mcp-Name` header labels a body value, and the param carrying it.
_NAME_BEARING_METHODS = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
}

# Non-ASCII values travel base64 between these markers, lowercase as required.
_NAME_B64_PREFIX = "=?base64?"
_NAME_B64_SUFFIX = "?="


def _decode_standard_name(raw: str) -> str:
    """Return the value `Mcp-Name` carries, decoding the base64 sentinel form."""
    if raw.startswith(_NAME_B64_PREFIX) and raw.endswith(_NAME_B64_SUFFIX):
        encoded = raw[len(_NAME_B64_PREFIX) : -len(_NAME_B64_SUFFIX)]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HeaderMismatchError("Mcp-Name is not valid base64") from exc
    if raw != raw.strip() or not raw.isascii() or not raw.isprintable():
        raise HeaderMismatchError("Mcp-Name carries characters that must travel base64-encoded")
    return raw


def _require_header(request: Request, name: str) -> str:
    """Return a required standard header, rejecting the request when it is absent."""
    value: str | None = request._peek_header_key(name.lower().encode("latin-1"))
    if value is None:
        raise HeaderMismatchError(f"{name} is required on this protocol revision")
    return value


def _needs_stream(
    server: MCPServer, message: dict[str, Any], event_store: SSEEventStore | None
) -> bool:
    """Whether this request can produce anything beyond its own single reply.

    The spec lets a POST carrying a request be answered either as JSON or as an SSE
    stream, and tells clients to accept both - so keying the choice on the `Accept`
    header alone means a conformant client always pays for stream framing, including
    for a call that emits exactly one message. Measured, that framing is a third of
    the cost of a plain tool call.

    A `tools/call` produces extra messages only when the handler can reach the
    `MCPContext`, which the registry settles per tool at registration. A handler
    reaching the connection WITHOUT binding one - through
    `MCPServer.send_to_current_connection` - is not visible to that walk and should
    be registered with `dispatches_tools=True`. Everything else keeps the stream.
    """
    if event_store is not None:
        return True
    if getattr(server, "_connections", None) is not None:
        # `GET` only resumes, so an open POST stream is the only delivery window.
        return True
    if message.get("method") != "tools/call":
        return True
    params = message.get("params")
    if not isinstance(params, dict):
        return True
    if "task" in params:
        # A task emits `notifications/tasks/status` after its reply.
        return True
    meta = params.get("_meta")
    if isinstance(meta, dict) and meta.get("progressToken") is not None:
        return True
    name = params.get("name")
    if not isinstance(name, str):
        return True
    tool = server.registry.resolve(name, _requested_version(params))
    return tool is None or tool.may_stream


def _validate_standard_headers(request: Request, message: dict[str, Any]) -> None:
    """Cross-check the standard request headers against the body they label.

    A modern request states its method - and, for the three methods that act on a
    named thing, that name - in headers as well as in the body, so a proxy can route
    without parsing JSON. The server executes the body, so the two MUST agree (MCP
    2026-07-28 Streamable HTTP, "Standard Request Headers" and "Server Validation").

    Earlier revisions defined none of these headers, so a handshake-era request is
    left alone; the request is modern when either end names a modern revision,
    because a header a proxy trusted is exactly what must not go unchecked.
    """
    method = message.get("method")
    if not isinstance(method, str):
        return
    header_version = request._peek_header_key(b"mcp-protocol-version")
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    raw_body_version = meta.get(META_PROTOCOL_VERSION) if isinstance(meta, dict) else None
    body_version = raw_body_version if isinstance(raw_body_version, str) else None
    # Gated on the revision each half names, not on a `_meta` version's presence.
    if not is_modern_version(header_version) and not is_modern_version(body_version):
        return

    if not is_modern_version(header_version):
        raise HeaderMismatchError("MCP-Protocol-Version is required on this protocol revision")
    if body_version is not None and body_version != header_version:
        raise HeaderMismatchError(
            "MCP-Protocol-Version does not match the version in the request body"
        )

    if _require_header(request, "mcp-method") != method:
        raise HeaderMismatchError("Mcp-Method does not match the method in the request body")

    field = _NAME_BEARING_METHODS.get(method)
    if field is None:
        return
    body_name = params.get(field) if isinstance(params, dict) else None
    if _decode_standard_name(_require_header(request, "mcp-name")) != body_name:
        raise HeaderMismatchError(f"Mcp-Name does not match params.{field} in the request body")


# ── Refusal responses ─────────────────────────────────────


def _method_not_allowed() -> Response:
    """Answer a GET on the MCP endpoint with HTTP 405 (no server-push stream)."""
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})


def _forbidden(response: dict[str, Any], scopes: list[str]) -> Response:
    """Build an HTTP 403 with a `WWW-Authenticate` insufficient-scope challenge."""
    parts = ['Bearer error="insufficient_scope"']
    if scopes:
        parts.append(f'scope="{" ".join(scopes)}"')
    return _protocol_response(
        response,
        status_code=status.HTTP_403_FORBIDDEN,
        headers={"WWW-Authenticate": ", ".join(parts)},
    )


# ── SSE streaming ─────────────────────────────────────────


def _stream_response(
    server: MCPServer,
    message: dict[str, Any],
    principal: Principal | None,
    session: MCPSession,
    session_id: str | None = None,
    event_store: SSEEventStore | None = None,
    store: HttpSessionStore | None = None,
) -> EventSourceResponse:
    """Answer one request as an SSE stream: its notifications then its response.

    The request is dispatched in a background task whose context carries an outbound
    sink wired to a queue; the SSE generator drains that queue. Per-request
    `_notifier_var` scoping keeps concurrent HTTP calls from crossing notifications.

    For the open lifetime of the stream the connection is registered with the server
    keyed by its `session`, with this stream's sink as its outbound channel, so an
    application-signalled `notifications/resources/updated` reaches a client that
    has `resources/subscribe`d; it is unregistered when the stream closes.

    The stream opens with a priming event so the first frame is addressable, and
    closes with a `retry` field hinting the reconnect delay (MCP 2025-11-25
    transport). The dispatch task is left running on client disconnection rather
    than cancelled - the spec forbids treating a dropped connection as a cancel.

    Under resumability each payload event is recorded before being queued, so a
    client that drops can replay the tail via a `Last-Event-ID` GET. The runner
    records regardless of the generator's state, so events produced after a
    disconnect are still available.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()
    # A disconnect does not cancel the call, so a chatty tool would buffer forever.
    draining = True
    # A `subscriptions/listen` is answered by its own close, nothing else.
    client_gone = asyncio.Event()
    # `seq` advances per payload; 0 is the priming event, so the replay base is known.
    stream_id = secrets.token_urlsafe(_STREAM_ID_ENTROPY_BYTES) if event_store is not None else None
    seq = count(1)

    def emit_id(payload: dict[str, Any]) -> str | None:
        if event_store is None or stream_id is None:
            return None
        return event_store.record(stream_id, next(seq), payload)

    # The HTTP transport's `Transport.send`; the recording survives a dropped stream.
    async def send(message: dict[str, Any]) -> None:
        event_id = emit_id(message)
        if draining:
            await queue.put((event_id, message))

    async def runner() -> None:
        token = _notifier_var.set(send)
        conn_token = server.register_connection(session, send)
        # Re-bound explicitly, so identity holds however the task was scheduled.
        set_principal(principal)
        try:
            response = await server.handle_message(message, session)
            # `initialize` lands here too, with the capabilities to persist.
            if store is not None and session_id is not None:
                await store.persist(session_id, session)
            if response is not None:
                response_id = emit_id(response)
                if draining:
                    await queue.put((response_id, response))
            elif message.get("id") in session.listen_streams:
                # Held until the client goes away, so the fan-out has a target.
                await client_gone.wait()
        except asyncio.CancelledError:
            # Cancelled deliberately: close without an error frame, as it expects.
            pass
        except Exception:
            _logger.exception("MCP HTTP request handling failed")
            err = _error(message.get("id"), _JSONRPC_INTERNAL_ERROR, "internal error")
            err_id = emit_id(err)
            if draining:
                await queue.put((err_id, err))
        finally:
            server.unregister_connection(conn_token)
            _notifier_var.reset(token)
            if draining:
                await queue.put(_STREAM_END)

    async def events() -> AsyncIterator[ServerSentEvent]:
        nonlocal draining
        # Disconnection is not cancellation (MCP transport); the binding holds it.
        runner_task = asyncio.ensure_future(runner())
        # An id plus empty data marks the stream open; `SSEEventStore` owns the sep.
        prime_id = f"{stream_id}{_EVENT_ID_SEP}0" if stream_id is not None else str(next(_event_id))
        try:
            yield ServerSentEvent(data="", id=prime_id)
            while True:
                item = await queue.get()
                if item is _STREAM_END:
                    break
                event_id, payload = item
                yield ServerSentEvent(data=encode_envelope(payload).decode(), id=event_id)
            yield ServerSentEvent(retry=_SSE_RETRY_MS)
            await runner_task
        finally:
            # Nothing will read this queue again; release a waiting listen stream too.
            draining = False
            client_gone.set()
            while not queue.empty():
                queue.get_nowait()

    headers = {_SESSION_ID_HEADER: session_id} if session_id is not None else None
    return EventSourceResponse(events(), headers=headers)
