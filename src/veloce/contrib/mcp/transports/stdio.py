"""JSON-RPC 2.0 over stdio — newline-delimited JSON on stdin / stdout.

Each line on stdin is one JSON-RPC request object; each response is written
as one JSON line to stdout. This is the framing an MCP client launching the
server as a subprocess expects. The transport never blocks the event loop:
stdin reads run in a thread executor (stdlib `sys.stdin` is blocking), and
the dispatch + write happen on the loop.

The duplex pipe also makes this the bidirectional transport: it satisfies
`BidirectionalTransport` via `request`, which sends a server->client request
(`sampling` / `elicitation` / `roots`) and awaits the client's correlated
reply. The serve loop reads that reply and settles the waiting future, so a
handler never reads the stream itself and a task-augmented call may sample,
elicit and list roots like any other.

**The loop is the sole reader and dispatches off itself**, so it keeps consuming
input while handlers run: `notifications/cancelled` reaches a call that is still
running, and `ping` is answered without queueing behind a slow tool. Ordinary
requests are chained so they still execute in the order they arrived - a client
that sends `logging/setLevel` before a call expects the level to be in force -
while those two control methods deliberately bypass the chain, since their whole
purpose is to reach a request that is already running.

`StdioTransport` is decoupled from the real streams - it takes an async
`read_line` source and an async `write_line` sink - so a test can drive a
full request/response round-trip in-process without touching real file
descriptors.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Iterator
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

# Prefix for server-issued request ids so a server->client request never collides
# with a client-issued id (the client owns its own id space; the server owns this).
_SERVER_ID_PREFIX = "srv-"

# Answered off the ordering chain. Everything else keeps strict request order -
# a client that sends `logging/setLevel` then a call expects the level to be in
# force - but these two exist to reach a request that is ALREADY running, so
# queueing them behind it defeats them entirely.
_CONTROL_METHODS = frozenset({"ping", "notifications/cancelled"})


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
        # Server->client requests awaiting a reply, keyed by the server-issued id.
        # Empty until a tool issues `sample` / `elicit` / `roots`, so a server that
        # never initiates a request holds nothing.
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._server_ids = count(1)
        # The live connection's session, exposed so `request` reads the client's
        # advertised capabilities; bound for the serve loop's lifetime.
        self._session: MCPSession | None = None
        # Dispatches run concurrently, so two could otherwise interleave halves
        # of a line. The framing is one JSON message per line, so a torn write is
        # an unparseable message for the client.
        self._write_lock = asyncio.Lock()
        # Tail of the ordered dispatch chain: each ordinary request awaits the
        # one ahead of it, so order survives without the read loop blocking.
        self._serial_tail: asyncio.Task[None] | None = None

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
        # Names the transport for `MCPContext.transport`; set once for the
        # serve task, whose context every dispatched call inherits.
        _transport_var.set("stdio")
        self.server.set_requester(self.request)
        # One session for the connection's lifetime: it records the client's
        # advertised capabilities from `initialize` and lets the server enforce
        # that no other request precedes initialization.
        session = MCPSession()
        self._session = session
        # Register this connection so an application-signalled resource change can
        # be delivered to it (a no-op when resource subscriptions are disabled);
        # unregistered on EOF so a closed connection receives nothing further.
        token = self.server.register_connection(session, self.send)
        # Requests are dispatched as tasks so this loop stays the SOLE reader and
        # keeps consuming input while a handler runs. Awaiting each dispatch made
        # `notifications/cancelled` unreachable - it could only be read once the
        # request it cancels had already finished - and queued `ping` behind a
        # slow tool.
        inflight: set[asyncio.Task[None]] = set()
        try:
            while True:
                line = await self._read_line()
                if line is None:
                    # The client closed its write side. Nothing more can be
                    # read, so a handler parked on a server->client reply would
                    # wait for a message that can never arrive - fail those
                    # first, or the drain below deadlocks on it.
                    self._fail_pending()
                    # Then let the dispatches already running finish and write
                    # their replies rather than dropping answers to requests the
                    # client did send.
                    if inflight:
                        await asyncio.gather(*inflight, return_exceptions=True)
                    return
                message, error = self._decode(line)
                if error is not None:
                    await self._emit(error)
                    continue
                if message is None:
                    continue
                # A reply to a server->client request settles the waiting future
                # rather than entering dispatch. Synchronous and cheap, so it is
                # never worth a task.
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
            # Only reached with work outstanding when the serve loop itself was
            # cancelled; a clean EOF has already drained above.
            for task in inflight:
                task.cancel()
            self.server.unregister_connection(token)
            # Reclaim what the connection owned, the same way the HTTP session
            # store does on idle eviction. Unregistering alone drops the
            # notification sink and the listen streams but leaves the session's
            # tasks registered, and `TaskRegistry.evict_expired` deliberately
            # never reaps a task that has not settled - so a never-settling task
            # created here outlived the connection, together with its running
            # asyncio runner, for the lifetime of the process.
            self.server.evict_session(session)
            self._session = None

    async def send(self, message: dict[str, Any]) -> None:
        """Write one server-initiated JSON-RPC message line to the client."""
        await self._emit(message)

    def _fail_pending(self) -> None:
        """Settle every waiting server->client request as failed.

        Called at EOF: the loop is the only reader, so once input is closed no
        correlated reply can arrive and a handler awaiting one would hang for
        the process lifetime.
        """
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(MCPRequestError("connection closed before reply"))
        self._pending.clear()

    async def _emit(self, payload: dict[str, Any]) -> None:
        """Write one JSON line, serialised against every other writer.

        `default=` is the same fallback the HTTP path uses, so one handler
        answering both doors produces the same JSON either way. Without it a
        `Decimal`, a `set`, a `Path` or a registered encoder - all of which
        `ctx.result_meta` puts straight into the envelope - raised here instead.
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
            # A value no encoder can represent must not cost the client its
            # reply. This runs in a task nothing awaits, so the exception would
            # otherwise be the whole of the diagnostic: no bytes written, no
            # error, and a client waiting for the lifetime of the process.
            _logger.exception("MCP stdio reply could not be encoded")
            await self._emit(internal_error(message.get("id"), "Response could not be serialised"))

    async def _dispatch_in_order(
        self,
        previous: asyncio.Task[None] | None,
        message: dict[str, Any],
        session: MCPSession,
    ) -> None:
        """Dispatch after `previous`, preserving the order requests arrived in.

        The read loop hands each request to a task so it can carry on reading,
        which is what lets a cancellation reach a running call. Left unordered
        that would also let a call overtake the `logging/setLevel` or
        `initialize` sent before it. A failure in the predecessor is its own
        business and must not stop this one.
        """
        if previous is not None and not previous.done():
            with contextlib.suppress(BaseException):
                await asyncio.shield(previous)
        await self._dispatch(message, session)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue a server->client request and await the client's correlated reply.

        Sends the JSON-RPC request and awaits the future the serve loop settles
        when the correlated reply arrives. Returns the reply's `result`; an error
        reply raises `MCPRequestError`.

        This does not read the stream itself: the serve loop is the sole
        reader. Reading here would make the loop and the calling handler two
        readers of one blocking stream, which cannot both be live - and both
        are live during a task-augmented call. Because there is only one
        reader, a task-augmented tool may sample, elicit and list roots like
        any other.
        """
        request_id = f"{_SERVER_ID_PREFIX}{next(self._server_ids)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._emit({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
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
            # It parsed, so the failure is the shape, not the JSON. JSON-RPC keeps
            # these apart: -32700 says the text could not be read, -32600 says what
            # was read is not a Request object. A batch array lands here too, since
            # the revisions this server speaks do not carry batches.
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


class MCPRequestError(Exception):
    """A server->client request failed (the client replied with an error or closed)."""


_STDIN_FD = 0
_STDOUT_FD = 1
_STDERR_FD = 2

# Set while a stdio server holds the process wire. Two servers on one process
# would each divert the other's descriptors, so the second is refused instead.
_wire_claimed = False


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
def _isolated_wire() -> Iterator[tuple[IO[bytes], IO[bytes]]]:
    """Yield the protocol (reader, writer) with descriptors 0 and 1 pointed away.

    While a stdio server is running the process's standard output *is* the
    protocol pipe, so anything else written there - a `print` left in a handler,
    a library that logs to stdout, a subprocess a tool spawns - lands in the
    newline-delimited JSON stream as a line the client cannot parse. The client
    reports malformed JSON, which points at everything except the write that
    caused it.

    The isolation has to be at the descriptor level: a child process inherits
    descriptors rather than Python file objects, so rebinding `sys.stdout` would
    not cover a tool that shells out. The wire is duplicated onto private
    descriptors (not inherited, per PEP 446), descriptor 0 is pointed at the null
    device and descriptor 1 at stderr, and both are restored on the way out. A
    stray write then shows up as diagnostics on stderr instead of corrupting the
    protocol, and a child no longer inherits the server's end of either pipe -
    which on Windows is also what stops it blocking in interpreter startup behind
    the server's pending read (CPython gh-78961).

    Every failure path degrades to serving the streams as they are, because a
    half-diverted wire is worse than an unisolated one.
    """
    sys.stdout.flush()
    try:
        wire_in = os.dup(_STDIN_FD)
        wire_out = os.dup(_STDOUT_FD)
    except OSError:
        # Nothing to duplicate: an embedded interpreter, or a standard stream
        # already closed. Serve on the streams as they are rather than not at all.
        yield sys.stdin.buffer, sys.stdout.buffer
        return

    null_fd = -1
    diverted = False
    try:
        try:
            null_fd = os.open(os.devnull, os.O_RDWR)
            os.dup2(null_fd, _STDIN_FD)
            # Stderr is where a stray write belongs; with no stderr to divert to,
            # the null device at least keeps it off the wire.
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
            # `sys.stdout` buffers, and its buffer is flushed to whatever
            # descriptor 1 points at when the flush happens - so a `print` left
            # unflushed by a handler would be written to the wire the moment it
            # is restored, or at interpreter exit. Drain it while it still
            # drains to stderr.
            with contextlib.suppress(Exception):
                sys.stdout.flush()
            _restore_wire(wire_in, wire_out)
        for fd in (wire_in, wire_out, null_fd):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)


async def serve_stdio(server: MCPServer) -> None:
    """Serve `server` over the real process stdin / stdout.

    The wire is isolated from the rest of the process for the duration: the
    protocol is carried on private duplicates of descriptors 0 and 1 while the
    standard ones point at the null device and at stderr, so a handler that
    prints, logs to stdout or spawns a child cannot corrupt the JSON-RPC stream.

    Blocking stdin reads are offloaded to the default thread executor so the
    event loop stays responsive; stdout writes are flushed per line so a
    client reading the pipe sees each response immediately.
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
