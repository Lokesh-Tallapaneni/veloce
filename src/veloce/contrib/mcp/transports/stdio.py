"""JSON-RPC 2.0 over stdio — newline-delimited JSON on stdin / stdout.

Each line on stdin is one JSON-RPC request object; each response is written
as one JSON line to stdout. This is the framing an MCP client launching the
server as a subprocess expects. The transport never blocks the event loop:
stdin reads run in a thread executor (stdlib `sys.stdin` is blocking), and
the dispatch + write happen on the loop.

The duplex pipe also makes this the bidirectional transport: `request` sends a
server->client request (`sampling` / `elicitation` / `roots`) and awaits the
reply the serve loop settles, so a handler never reads the stream itself.

**The loop is the sole reader and dispatches off itself**, so it keeps consuming
input while handlers run: `notifications/cancelled` reaches a call that is still
running, and `ping` is answered without queueing behind a slow tool. Ordinary
requests are chained so they still execute in the order they arrived, while
those two control methods bypass the chain by design.

`StdioTransport` takes an async `read_line` source and an async `write_line`
sink, so a test can drive a round-trip in-process without real descriptors.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Generator
from itertools import count
from typing import IO, TYPE_CHECKING, Any

import orjson

from veloce.contrib.mcp._helpers import encode_envelope
from veloce.contrib.mcp.context import _transport_var
from veloce.contrib.mcp.errors import internal_error, invalid_request_error, parse_error
from veloce.contrib.mcp.session import MCPSession

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MCPServer
    from veloce.contrib.mcp.transports.base import BidirectionalTransport

_logger = logging.getLogger(__name__)

# So a server-issued request id never collides with a client-issued one.
_SERVER_ID_PREFIX = "srv-"

# Answered off the ordering chain: these two exist to reach a request that is
# ALREADY running, so queueing them behind it defeats them.
_CONTROL_METHODS = frozenset({"ping", "notifications/cancelled"})


# ── The transport ─────────────────────────────────────────


class StdioTransport:
    """Drive an `MCPServer` over a line-delimited JSON byte stream.

    Satisfies `BidirectionalTransport`: `send` writes one outbound JSON-RPC
    message line (wired as the server's notification sink) and `request` issues a
    server->client request, awaiting the client's correlated reply read by the
    serve loop.
    """

    __slots__ = (
        "server",
        "_read_line",
        "_write_line",
        "_pending",
        "_server_ids",
        "_session",
        "_write_lock",
        "_serial_tail",
    )

    def __init__(
        self,
        server: MCPServer,
        read_line: Callable[[], Awaitable[bytes | None]],
        write_line: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self.server = server
        self._read_line = read_line
        self._write_line = write_line
        # Server->client requests awaiting a reply, keyed by server-issued id.
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._server_ids = count(1)
        # Bound for the serve loop's lifetime, so `request` reads its capabilities.
        self._session: MCPSession | None = None
        # One JSON message per line, so a torn write is unparseable.
        self._write_lock = asyncio.Lock()
        # Each request awaits the one ahead, so order survives a non-blocking loop.
        self._serial_tail: asyncio.Task[None] | None = None

    async def serve(self) -> None:
        """Read, dispatch, and reply line-by-line until the input closes.

        A blank line is skipped; an unparseable line yields a JSON-RPC parse
        error; a notification (no response) writes nothing; a reply to a pending
        server->client request resolves it instead of dispatching. The loop ends
        when `read_line` returns `None` (EOF).

        Framing is newline-delimited per the MCP stdio transport spec
        ("messages are delimited by newlines, and MUST NOT contain embedded
        newlines"). MCP does NOT use LSP-style `Content-Length:` header framing;
        do not "fix" this into it.
        """
        # The outbound channel for a handler's notifications and requests.
        self.server.set_notifier(self.send)
        # Set once for the serve task, whose context every call inherits.
        _transport_var.set("stdio")
        self.server.set_requester(self.request)
        # One per connection, so nothing may precede `initialize`.
        session = MCPSession()
        self._session = session
        # So an application-signalled resource change reaches this connection.
        token = self.server.register_connection(session, self.send)
        # Dispatched as tasks so this loop stays the SOLE reader and keeps
        # consuming input while a handler runs.
        inflight: set[asyncio.Task[None]] = set()
        try:
            while True:
                line = await self._read_line()
                if line is None:
                    # A handler parked on a reply must fail first, or the
                    # drain below deadlocks on it.
                    self._fail_pending()
                    # Then let running dispatches finish and write their replies.
                    if inflight:
                        await asyncio.gather(*inflight, return_exceptions=True)
                    return
                message, error = self._decode(line)
                if error is not None:
                    await self._emit(error)
                    continue
                if message is None:
                    continue
                # Synchronous and cheap, so never worth a task.
                if "method" not in message:
                    self._resolve_reply(message)
                    continue
                loop = asyncio.get_running_loop()
                if message.get("method") in _CONTROL_METHODS:
                    task = loop.create_task(self._dispatch(message, session))
                else:
                    task = loop.create_task(
                        self._dispatch_in_order(self._serial_tail, message, session)
                    )
                    self._serial_tail = task
                inflight.add(task)
                task.add_done_callback(inflight.discard)
        finally:
            # Outstanding work here means `serve` was cancelled; EOF has drained.
            for task in inflight:
                task.cancel()
            self.server.unregister_connection(token)
            # Unregistering leaves the session's tasks registered, and
            # `evict_expired` never reaps an unsettled one.
            self.server.evict_session(session)
            self._session = None

    async def send(self, message: dict[str, Any]) -> None:
        """Write one server-initiated JSON-RPC message line to the client."""
        await self._emit(message)

    def _fail_pending(self) -> None:
        """Settle every waiting server->client request as failed.

        Called at EOF: the loop is the only reader, so no correlated reply can
        arrive and a handler awaiting one would hang for the process lifetime.
        """
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(MCPRequestError("connection closed before reply"))
        self._pending.clear()

    async def _emit(self, payload: dict[str, Any]) -> None:
        """Write one JSON line, serialised against every other writer.

        `default=` is the same fallback the HTTP path uses, so one handler
        answering both doors produces the same JSON either way.
        """
        async with self._write_lock:
            await self._write_line(encode_envelope(payload))

    async def _dispatch(self, message: dict[str, Any], session: MCPSession) -> None:
        """Answer one request off the read loop, so the loop keeps reading."""
        response = await self.server.handle_message(message, session)
        if response is None:
            return
        try:
            await self._emit(response)
        except TypeError:
            # In a task nothing awaits, so an unencodable value would otherwise
            # write no bytes and leave the client waiting.
            _logger.exception("MCP stdio reply could not be encoded")
            await self._emit(internal_error(message.get("id"), "Response could not be serialised"))

    async def _dispatch_in_order(
        self,
        previous: asyncio.Task[None] | None,
        message: dict[str, Any],
        session: MCPSession,
    ) -> None:
        """Dispatch after `previous`, preserving the order requests arrived in.

        The read loop hands each request to a task so a cancellation can reach a
        running call; left unordered that would also let a call overtake the
        `logging/setLevel` or `initialize` sent before it. A failure in the
        predecessor must not stop this one.
        """
        if previous is not None and not previous.done():
            # `Exception`, not `BaseException`: a wider catch would absorb a
            # cancel of *this* task and dispatch against a reclaimed session.
            try:
                await asyncio.shield(previous)
            except asyncio.CancelledError:
                # A cancel of *this* task leaves the predecessor running.
                # `Task.cancelling()` would say the same, but only from 3.11.
                if not previous.cancelled():
                    raise
            except Exception:
                pass
        await self._dispatch(message, session)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue a server->client request and await the client's correlated reply.

        Sends the JSON-RPC request and awaits the future the serve loop settles
        when the correlated reply arrives. Returns the reply's `result`; an error
        reply raises `MCPRequestError`.

        This does not read the stream itself: the serve loop is the sole reader.
        Reading here would make it and the calling handler two readers of one
        blocking stream, which cannot both be live.
        """
        request_id = f"{_SERVER_ID_PREFIX}{next(self._server_ids)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        # Inside the `finally`'s reach, because `encode_envelope` raises on the
        # author-supplied `params` and the entry would otherwise leak.
        try:
            await self._emit(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await future
        except asyncio.CancelledError:
            raise MCPRequestError("connection closed before reply") from None
        finally:
            self._pending.pop(request_id, None)

    def _decode(self, line: bytes) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Parse one input line into `(message, error_response)`.

        Both are `None` for a blank line, which is skipped; otherwise exactly one
        is set, so the caller routes on whichever it got.
        """
        stripped = line.strip()
        if not stripped:
            return None, None
        try:
            message = orjson.loads(stripped)
        except orjson.JSONDecodeError:
            return None, parse_error()
        if not isinstance(message, dict):
            # JSON-RPC: -32700 is unreadable text, -32600 a non-Request object.
            return None, invalid_request_error()
        return message, None

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


# ── Errors ────────────────────────────────────────────────


class MCPRequestError(Exception):
    """A server->client request failed (the client replied with an error or closed)."""


_STDIN_FD = 0
_STDOUT_FD = 1
_STDERR_FD = 2

# Two servers on one process would each divert the other's descriptors.
_wire_claimed = False


# ── Wire isolation ────────────────────────────────────────


def _descriptor_is_open(fd: int) -> bool:
    """Whether `fd` refers to something this process can still write to."""
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _restore_wire(wire_in: int, wire_out: int) -> None:
    """Point the standard descriptors back at the pipes they started on."""
    for source, target in ((wire_in, _STDIN_FD), (wire_out, _STDOUT_FD)):
        with contextlib.suppress(OSError):
            os.dup2(source, target)


@contextlib.contextmanager
def _isolated_wire() -> Generator[tuple[IO[bytes], IO[bytes]], None, None]:
    """Yield the protocol (reader, writer) with descriptors 0 and 1 pointed away.

    While a stdio server is running the process's standard output *is* the
    protocol pipe, so anything else written there - a stray `print`, a library
    logging to stdout, a subprocess a tool spawns - reaches the client as a line
    it cannot parse.

    The isolation has to be at the descriptor level, because a child inherits
    descriptors rather than Python file objects: the wire is duplicated onto
    private descriptors (not inherited, per PEP 446), descriptor 0 points at the
    null device and descriptor 1 at stderr, and both are restored on the way out.
    A child then no longer inherits the server's end of either pipe, which on
    Windows is also what stops it blocking in interpreter startup behind the
    server's pending read (CPython gh-78961).

    Every failure path degrades to serving the streams as they are, because a
    half-diverted wire is worse than an unisolated one.
    """
    sys.stdout.flush()
    try:
        wire_in = os.dup(_STDIN_FD)
        wire_out = os.dup(_STDOUT_FD)
    except OSError:
        # An embedded interpreter, or a stream already closed.
        yield sys.stdin.buffer, sys.stdout.buffer
        return

    null_fd = -1
    diverted = False
    try:
        try:
            null_fd = os.open(os.devnull, os.O_RDWR)
            os.dup2(null_fd, _STDIN_FD)
            # With no stderr to divert to, the null device keeps it off the wire.
            os.dup2(_STDERR_FD if _descriptor_is_open(_STDERR_FD) else null_fd, _STDOUT_FD)
            diverted = True
        except OSError:
            _restore_wire(wire_in, wire_out)
        if not diverted:
            yield sys.stdin.buffer, sys.stdout.buffer
            return
        with (
            os.fdopen(wire_in, "rb", closefd=False) as reader,
            os.fdopen(wire_out, "wb", closefd=False) as writer,
        ):
            yield reader, writer
    finally:
        if diverted:
            # Flushed to whatever descriptor 1 points at, so drain it while
            # that is still stderr.
            with contextlib.suppress(Exception):
                sys.stdout.flush()
            _restore_wire(wire_in, wire_out)
        for fd in (wire_in, wire_out, null_fd):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)


async def serve_stdio(server: MCPServer) -> None:
    """Serve `server` over the real process stdin / stdout.

    The wire is isolated from the rest of the process for the duration, so a
    handler that prints, logs to stdout or spawns a child cannot corrupt the
    JSON-RPC stream. Blocking stdin reads are offloaded to the default thread
    executor; stdout writes are flushed per line.
    """
    global _wire_claimed
    if _wire_claimed:
        raise RuntimeError(
            "a stdio MCP server is already serving this process; the standard "
            "descriptors carry one protocol stream and cannot carry two."
        )

    loop = asyncio.get_running_loop()
    _wire_claimed = True
    try:
        with _isolated_wire() as (stdin, stdout):
            await _serve_on(server, loop, stdin, stdout)
    finally:
        _wire_claimed = False


async def _serve_on(
    server: MCPServer, loop: asyncio.AbstractEventLoop, stdin: IO[bytes], stdout: IO[bytes]
) -> None:
    """Run the transport's read / write loop over one pair of byte streams."""

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
