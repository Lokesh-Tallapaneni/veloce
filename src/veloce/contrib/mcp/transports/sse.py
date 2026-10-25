"""Legacy HTTP+SSE transport — the split-endpoint wire of MCP revision 2024-11-05.

Two endpoints: a long-lived `GET` carrying everything the server says, and a
`POST` carrying everything the client says. The client learns the POST URL from
the stream's first `endpoint` event, which carries the session id tying the two
halves together.

The asymmetry is the whole design: a `POST` is answered `202 Accepted` with no
body, and its JSON-RPC response arrives later as a `message` event on the open
stream, so a client not listening to its stream never sees its answers.

Deprecated in favour of Streamable HTTP (`transports/http.py`); prefer
`mount_mcp(transport="http")` for anything new.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import veloce.status as status
from veloce._protocol_constants import HTTP_METHOD_GET, HTTP_METHOD_POST
from veloce.contrib.mcp._helpers import (
    _notifier_var,
    encode_envelope,
    transport_route_name,
)
from veloce.contrib.mcp._posture import record_endpoint
from veloce.contrib.mcp.context import _transport_var
from veloce.contrib.mcp.errors import (
    _JSONRPC_INTERNAL_ERROR,
    MCPError,
    SessionNotFoundError,
    SessionRequiredError,
    _error,
    invalid_request_error,
    parse_error,
)
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.transports._common import (
    _SESSION_ID_ENTROPY_BYTES,
    _SSE_RETRY_MS,
    _authenticate,
    _protocol_response,
    _validate_origin,
    register_metadata_route,
)
from veloce.http.response import Response
from veloce.principal import Principal, current_principal, set_principal
from veloce.sse import EventSourceResponse, ServerSentEvent

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from veloce.contrib.mcp.auth import MCPAuth
    from veloce.contrib.mcp.server import MCPServer
    from veloce.http.request import Request

_logger = logging.getLogger(__name__)

# The name the 2024-11-05 transport defines; a client waits for it to speak.
_ENDPOINT_EVENT = "endpoint"

# The event carrying a JSON-RPC message from server to client.
_MESSAGE_EVENT = "message"


# ── Open connections ──────────────────────────────────────


class _SSEConnection:
    """One open stream: its session, its outbound queue, and its caller."""

    __slots__ = ("session", "queue", "principal")

    def __init__(self, session: MCPSession, principal: Principal | None) -> None:
        self.session = session
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        # Who opened the stream, which a POST without its own credential runs as.
        self.principal = principal

    async def send(self, message: dict[str, Any]) -> None:
        """Queue a message for this connection's stream - the `Transport.send`."""
        await self.queue.put(message)


# ── Route registration ────────────────────────────────────


def register_sse_transport(
    app: Any,
    server: MCPServer,
    path: str = "/sse",
    message_path: str = "/messages",
    auth: MCPAuth | None = None,
    allowed_origins: frozenset[str] | None = None,
    exclude_middleware: Sequence[str] | None = None,
) -> None:
    """Mount the legacy split-endpoint SSE transport for `server` on `app`.

    `path` opens the stream; `message_path` receives the client's messages. The
    client is told the second by the first, so the two only have to agree here.

    `auth` makes both endpoints an OAuth 2.1 resource server, and
    `allowed_origins` enables `Origin` validation (DNS-rebinding defense).
    """
    connections: dict[str, _SSEConnection] = {}

    async def open_stream(request: Request) -> Response:
        try:
            _validate_origin(request, allowed_origins)
        except MCPError as exc:
            return _protocol_response(exc.to_error(None), status_code=exc.http_status)
        principal = None
        if auth is not None:
            principal, challenge = await _authenticate(auth, request)
            if challenge is not None:
                return challenge

        session_id = secrets.token_urlsafe(_SESSION_ID_ENTROPY_BYTES)
        connection = _SSEConnection(MCPSession(), principal)
        connections[session_id] = connection
        return EventSourceResponse(
            _stream(server, connections, session_id, connection, message_path)
        )

    async def receive_message(request: Request) -> Response:
        try:
            _validate_origin(request, allowed_origins)
        except MCPError as exc:
            return _protocol_response(exc.to_error(None), status_code=exc.http_status)
        if auth is not None:
            principal, challenge = await _authenticate(auth, request)
            if challenge is not None:
                return challenge
            # Unset, the tool would run as whoever opened the GET stream.
            set_principal(principal)

        session_id = request.query_params.get("sessionId")
        try:
            connection = _resolve(connections, session_id)
        except MCPError as exc:
            return _protocol_response(exc.to_error(None), status_code=exc.http_status)

        # An unreadable id has no stream frame, so the POST carries the failure.
        try:
            message = await request.json()
        except Exception:
            return _protocol_response(parse_error(), status_code=status.HTTP_400_BAD_REQUEST)
        if message is None:
            return _protocol_response(parse_error(), status_code=status.HTTP_400_BAD_REQUEST)
        if not isinstance(message, dict):
            return _protocol_response(
                invalid_request_error(), status_code=status.HTTP_400_BAD_REQUEST
            )

        # The answer travels on the stream, so this is acknowledged at once.
        connection.queue.put_nowait(_Dispatch(message, current_principal() or connection.principal))
        return Response(status_code=status.HTTP_202_ACCEPTED)

    app.add_route(
        path,
        open_stream,
        methods=[HTTP_METHOD_GET],
        include_in_schema=False,
        exclude_middleware=exclude_middleware,
        name=transport_route_name("open_stream", path),
    )
    app.add_route(
        message_path,
        receive_message,
        methods=[HTTP_METHOD_POST],
        include_in_schema=False,
        exclude_middleware=exclude_middleware,
        name=transport_route_name("receive_message", message_path),
    )
    register_metadata_route(app, auth, exclude_middleware)
    record_endpoint(app, "sse", path, auth, allowed_origins)


# ── Message dispatch ──────────────────────────────────────


class _Dispatch:
    """A message the POST half handed to the stream half to run and answer."""

    __slots__ = ("message", "principal")

    def __init__(self, message: dict[str, Any], principal: Principal | None) -> None:
        self.message = message
        self.principal = principal


def _resolve(connections: dict[str, _SSEConnection], session_id: str | None) -> _SSEConnection:
    """Return the connection a POST names, or raise the matching MCP error."""
    if not session_id:
        raise SessionRequiredError("missing sessionId query parameter")
    connection = connections.get(session_id)
    if connection is None:
        raise SessionNotFoundError("unknown or closed session")
    return connection


async def _stream(
    server: MCPServer,
    connections: dict[str, _SSEConnection],
    session_id: str,
    connection: _SSEConnection,
    message_path: str,
) -> AsyncIterator[ServerSentEvent]:
    """Yield the endpoint event, then every message the server sends this client.

    Each queued dispatch runs here, not in the POST handler, so its notifications
    are queued onto the same channel as its response - the ordering a client
    relies on.
    """
    endpoint = f"{message_path}?sessionId={quote(session_id, safe='')}"
    # First frame: the client cannot speak without it, and WHATWG SSE applies
    # `retry` as soon as it is parsed.
    yield ServerSentEvent(data=endpoint, event=_ENDPOINT_EVENT, retry=_SSE_RETRY_MS)

    conn_token = server.register_connection(connection.session, connection.send)
    pending: set[asyncio.Task[None]] = set()
    try:
        while True:
            item = await connection.queue.get()
            if isinstance(item, _Dispatch):
                task = asyncio.ensure_future(_run(server, connection, item))
                pending.add(task)
                task.add_done_callback(pending.discard)
                continue
            yield ServerSentEvent(data=encode_envelope(item).decode(), event=_MESSAGE_EVENT)
    finally:
        # The client is gone, so a later POST naming it is told rather than queued.
        connections.pop(session_id, None)
        server.unregister_connection(conn_token)
        for task in pending:
            task.cancel()
        # Unregistering leaves the tasks registered and `evict_expired` never
        # reaps an unsettled one; this session has no life beyond the stream.
        server.evict_session(connection.session)


async def _run(server: MCPServer, connection: _SSEConnection, dispatch: _Dispatch) -> None:
    """Handle one message, queueing its notifications and response on the stream."""
    transport_token = _transport_var.set("sse")
    token = _notifier_var.set(connection.send)
    set_principal(dispatch.principal)
    try:
        response = await server.handle_message(dispatch.message, connection.session)
        if response is not None:
            await connection.queue.put(response)
    except asyncio.CancelledError:
        raise
    except Exception:
        _logger.exception("MCP SSE request handling failed")
        await connection.queue.put(
            _error(dispatch.message.get("id"), _JSONRPC_INTERNAL_ERROR, "internal error")
        )
    finally:
        _notifier_var.reset(token)
        _transport_var.reset(transport_token)
