"""Legacy HTTP+SSE transport — the split-endpoint wire of MCP revision 2024-11-05.

Before Streamable HTTP, an MCP server over HTTP exposed *two* endpoints: a long
lived `GET` that opens an SSE stream carrying everything the server says, and a
`POST` that carries everything the client says. The client learns the POST URL
from the stream itself - the first event the server sends is an `endpoint` event
whose data is that URL, carrying the session id that ties the two halves
together.

The asymmetry is the whole design: a `POST` is answered `202 Accepted` with no
body, and the JSON-RPC response for it arrives later as a `message` event on the
open stream. A client that is not listening to its stream therefore never sees
its answers.

This transport is deprecated - the current spec defines Streamable HTTP
(`transports/http.py`), which does the same work over one endpoint and survives a
dropped connection. It is served here for clients that only speak the older wire.
Prefer `mount_mcp(transport="http")` for anything new.

Both halves converge on `MCPServer.handle_message`, exactly as the other
transports do, so tools, resources and prompts behave identically whichever wire
carries them.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from veloce import status
from veloce._protocol_constants import HTTP_METHOD_GET, HTTP_METHOD_POST
from veloce.contrib.mcp._helpers import _notifier_var
from veloce.contrib.mcp.errors import (
    _JSONRPC_INTERNAL_ERROR,
    MCPError,
    SessionNotFoundError,
    SessionRequiredError,
    _error,
)
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.transports.http import _authenticate, _logger, _validate_origin
from veloce.http.response import JSONResponse, Response
from veloce.principal import Principal, current_principal, set_principal
from veloce.sse import EventSourceResponse, ServerSentEvent

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from veloce.contrib.mcp.auth import MCPAuth
    from veloce.contrib.mcp.server import MCPServer
    from veloce.http.request import Request

# Bytes of entropy in a session id. The id appears in the endpoint URL the client
# is told to POST to, and it is the only thing tying a POST to the stream that
# will answer it, so it is unguessable rather than sequential.
_SESSION_ID_ENTROPY_BYTES = 24

# The event naming the URL to POST to. The 2024-11-05 transport defines exactly
# this name; a client waits for it before sending anything.
_ENDPOINT_EVENT = "endpoint"

# The event carrying a JSON-RPC message from server to client.
_MESSAGE_EVENT = "message"

# Reconnect hint, in milliseconds, sent when a stream closes.
_SSE_RETRY_MS = 3000

# Queued by the POST half to tell a stream generator to finish.
_STREAM_END = object()


class _SSEConnection:
    """One open stream: its session, its outbound queue, and its caller."""

    __slots__ = ("session", "queue", "principal")

    def __init__(self, session: MCPSession, principal: Principal | None) -> None:
        self.session = session
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        # The identity that opened the stream. A POST is authenticated in its own
        # right; this is what the dispatch task runs under when the POST carries
        # no credential of its own, which is the usual shape for this transport.
        self.principal = principal

    async def send(self, message: dict[str, Any]) -> None:
        """The `Transport.send` for this connection: queue a message for the stream."""
        await self.queue.put(message)


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
    `allowed_origins` enables `Origin` validation (DNS-rebinding defense) - the
    same options the Streamable HTTP transport takes, since both are ordinary
    HTTP endpoints on this app.
    """
    connections: dict[str, _SSEConnection] = {}

    async def open_stream(request: Request) -> Response:
        try:
            _validate_origin(request, allowed_origins)
        except MCPError as exc:
            return JSONResponse(exc.to_error(None), status_code=exc.http_status)
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
            return JSONResponse(exc.to_error(None), status_code=exc.http_status)
        if auth is not None:
            _principal, challenge = await _authenticate(auth, request)
            if challenge is not None:
                return challenge

        session_id = request.query_params.get("sessionId")
        try:
            connection = _resolve(connections, session_id)
        except MCPError as exc:
            return JSONResponse(exc.to_error(None), status_code=exc.http_status)

        try:
            message = await request.json()
        except Exception:
            message = None
        if not isinstance(message, dict):
            # There is no stream frame to carry a parse error for a message whose
            # id could not be read, so this one failure is reported on the POST.
            return JSONResponse(
                _error(None, _JSONRPC_INTERNAL_ERROR, "request body must be a JSON-RPC object"),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # The answer travels on the stream, not on this response, so the dispatch
        # runs detached and the POST is acknowledged immediately. This is the
        # defining shape of the transport.
        connection.queue.put_nowait(_Dispatch(message, current_principal() or connection.principal))
        return Response(status_code=status.HTTP_202_ACCEPTED)

    app.add_route(
        path,
        open_stream,
        methods=[HTTP_METHOD_GET],
        include_in_schema=False,
        exclude_middleware=exclude_middleware,
    )
    app.add_route(
        message_path,
        receive_message,
        methods=[HTTP_METHOD_POST],
        include_in_schema=False,
        exclude_middleware=exclude_middleware,
    )


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
) -> Any:
    """Yield the endpoint event, then every message the server sends this client.

    Each queued dispatch is run here rather than in the POST handler, so the work
    lives for as long as the stream does and its notifications are queued onto the
    same channel its response will use - the ordering a client relies on.
    """
    endpoint = f"{message_path}?sessionId={quote(session_id, safe='')}"
    # The client cannot speak until it has this, so it is the first frame.
    yield ServerSentEvent(data=endpoint, event=_ENDPOINT_EVENT)

    conn_token = server.register_connection(connection.session, connection.send)
    pending: set[asyncio.Task[None]] = set()
    try:
        while True:
            item = await connection.queue.get()
            if item is _STREAM_END:
                break
            if isinstance(item, _Dispatch):
                task = asyncio.ensure_future(_run(server, connection, item))
                pending.add(task)
                task.add_done_callback(pending.discard)
                continue
            yield ServerSentEvent.json(item, event=_MESSAGE_EVENT)
    finally:
        # The client is gone: drop the session so a later POST naming it is told
        # so, rather than queueing an answer nothing will read.
        connections.pop(session_id, None)
        server.unregister_connection(conn_token)
        for task in pending:
            task.cancel()
    yield ServerSentEvent(retry=_SSE_RETRY_MS)


async def _run(server: MCPServer, connection: _SSEConnection, dispatch: _Dispatch) -> None:
    """Handle one message, queueing its notifications and response on the stream."""
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
