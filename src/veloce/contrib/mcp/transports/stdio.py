"""JSON-RPC 2.0 over stdio — newline-delimited JSON on stdin / stdout.

Each line on stdin is one JSON-RPC request object; each response is written
as one JSON line to stdout. This is the framing an MCP client launching the
server as a subprocess expects. The transport never blocks the event loop:
stdin reads run in a thread executor (stdlib `sys.stdin` is blocking), and
the dispatch + write happen on the loop.

The duplex pipe also makes this the bidirectional transport: it satisfies
`BidirectionalTransport` via `request`, which sends a server->client request
(`sampling` / `elicitation` / `roots`) and awaits the client's correlated
reply. While a handler awaits such a request the serve loop keeps reading: an
inbound message that is the awaited reply resolves it, any other inbound message
is dispatched normally, so the client may interleave its own calls.

`StdioTransport` is decoupled from the real streams - it takes an async
`read_line` source and an async `write_line` sink - so a test can drive a
full request/response round-trip in-process without touching real file
descriptors.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from itertools import count
from typing import TYPE_CHECKING, Any

import orjson

from veloce.contrib.mcp.session import MCPSession

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MCPServer
    from veloce.contrib.mcp.transports.base import BidirectionalTransport

# JSON-RPC 2.0 Sec. 5.1 parse error - returned for a line that is not valid
# JSON. The id is null because the request could not be read.
_JSONRPC_PARSE_ERROR = -32700

# Prefix for server-issued request ids so a server->client request never collides
# with a client-issued id (the client owns its own id space; the server owns this).
_SERVER_ID_PREFIX = "srv-"


class StdioTransport:
    """Drive an `MCPServer` over a line-delimited JSON byte stream.

    Satisfies `BidirectionalTransport`: `send` writes one outbound JSON-RPC
    message line (wired as the server's notification sink) and `request` issues a
    server->client request, awaiting the client's correlated reply read by the
    serve loop.
    """

    __slots__ = ("server", "_read_line", "_write_line", "_pending", "_server_ids", "_session")

    def __init__(
        self,
        server: MCPServer,
        read_line: Callable[[], Awaitable[bytes | None]],
        write_line: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self.server = server
        self._read_line = read_line
        self._write_line = write_line
        # Server->client requests awaiting a reply, keyed by the server-issued id.
        # Empty until a tool issues `sample` / `elicit` / `roots`, so a server that
        # never initiates a request holds nothing.
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._server_ids = count(1)
        # The live connection's session, exposed so `request` reads the client's
        # advertised capabilities; bound for the serve loop's lifetime.
        self._session: MCPSession | None = None

    async def serve(self) -> None:
        """Read, dispatch, and reply line-by-line until the input closes.

        A blank line is skipped; an unparseable line yields a JSON-RPC parse
        error; a notification (no response) writes nothing; a reply to a pending
        server->client request resolves it instead of dispatching. The loop ends
        when `read_line` returns `None` (EOF).

        Framing is newline-delimited per the MCP stdio transport spec
        ("messages are delimited by newlines, and MUST NOT contain embedded
        newlines"). This is deliberate and correct: the MCP stdio transport
        does NOT use LSP-style `Content-Length:` header framing - that belongs
        to the Language Server Protocol, not MCP. One JSON line in, one JSON
        line out. Do not "fix" this into header framing.
        """
        # Wire the outbound one-way channel so a handler's progress / log
        # notifications reach the client, and the server->client request issuer so
        # a handler's `sample` / `elicit` / `roots` reaches it. The loop is serial,
        # so a direct write never races the loop's response write.
        self.server.set_notifier(self.send)
        self.server.set_requester(self.request)
        # One session for the connection's lifetime: it records the client's
        # advertised capabilities from `initialize` and lets the server enforce
        # that no other request precedes initialization.
        session = MCPSession()
        self._session = session
        # Register this connection so an application-signalled resource change can
        # be delivered to it (a no-op when resource subscriptions are disabled);
        # unregistered on EOF so a closed connection receives nothing further.
        self.server.register_connection(session, self.send)
        try:
            while True:
                line = await self._read_line()
                if line is None:
                    return
                response = await self._process_line(line, session)
                if response is not None:
                    await self._write_line(orjson.dumps(response))
        finally:
            self.server.unregister_connection(session)
            self._session = None

    async def send(self, message: dict[str, Any]) -> None:
        """Write one server-initiated JSON-RPC message line to the client."""
        await self._write_line(orjson.dumps(message))

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue a server->client request and await the client's correlated reply.

        Sends the JSON-RPC request, then reads inbound lines until the matching
        reply arrives: any other inbound message is dispatched and answered inline,
        so the client may interleave its own calls while the request is in flight.
        Returns the reply's `result`; an error reply raises `MCPRequestError`.
        """
        request_id = f"{_SERVER_ID_PREFIX}{next(self._server_ids)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write_line(
            orjson.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        )
        # Pump the input until this request's reply resolves the future. The serve
        # loop is blocked in the handler that called us, so reading here keeps the
        # connection live and lets the client interleave its own requests.
        try:
            while not future.done():
                line = await self._read_line()
                if line is None:
                    raise MCPRequestError("connection closed before reply")
                response = await self._process_line(line, self._session)
                if response is not None:
                    await self._write_line(orjson.dumps(response))
        finally:
            self._pending.pop(request_id, None)
        return future.result()

    async def _process_line(self, line: bytes, session: MCPSession | None) -> dict[str, Any] | None:
        """Route one input line: resolve a pending reply, or dispatch a request."""
        stripped = line.strip()
        if not stripped:
            return None
        try:
            message = orjson.loads(stripped)
        except orjson.JSONDecodeError:
            return self._parse_error()
        if not isinstance(message, dict):
            return self._parse_error()
        # A reply to a server->client request (an id we issued, no method) resolves
        # the waiting future rather than entering dispatch.
        if "method" not in message:
            self._resolve_reply(message)
            return None
        return await self.server.handle_message(message, session)

    def _resolve_reply(self, message: dict[str, Any]) -> None:
        """Settle the pending server->client request named by a reply's id."""
        reply_id = message.get("id")
        future = self._pending.get(reply_id) if isinstance(reply_id, str) else None
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            future.set_exception(MCPRequestError(str(error.get("message") or "request failed")))
        else:
            result = message.get("result")
            future.set_result(result if isinstance(result, dict) else {})

    @staticmethod
    def _parse_error() -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": _JSONRPC_PARSE_ERROR, "message": "Parse error"},
        }


class MCPRequestError(Exception):
    """A server->client request failed (the client replied with an error or closed)."""


async def serve_stdio(server: MCPServer) -> None:
    """Serve `server` over the real process stdin / stdout.

    Blocking stdin reads are offloaded to the default thread executor so the
    event loop stays responsive; stdout writes are flushed per line so a
    client reading the pipe sees each response immediately.
    """
    loop = asyncio.get_running_loop()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    async def read_line() -> bytes | None:
        line = await loop.run_in_executor(None, stdin.readline)
        # `readline` returns b"" only at EOF; a blank input line is b"\n".
        return None if line == b"" else line

    async def write_line(data: bytes) -> None:
        def _write() -> None:
            stdout.write(data + b"\n")
            stdout.flush()

        await loop.run_in_executor(None, _write)

    await StdioTransport(server, read_line, write_line).serve()


if TYPE_CHECKING:  # pragma: no cover
    # Static assertion that the transport satisfies the bidirectional contract.
    _: BidirectionalTransport = StdioTransport(None, None, None)  # type: ignore[arg-type]
