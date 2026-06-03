"""JSON-RPC 2.0 over stdio - newline-delimited JSON on stdin / stdout.

Each line on stdin is one JSON-RPC request object; each response is written
as one JSON line to stdout. This is the framing an MCP client launching the
server as a subprocess expects. The transport never blocks the event loop:
stdin reads run in a thread executor (stdlib `sys.stdin` is blocking), and
the dispatch + write happen on the loop.

`StdioTransport` is decoupled from the real streams - it takes an async
`read_line` source and an async `write_line` sink - so a test can drive a
full request/response round-trip in-process without touching real file
descriptors.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import orjson

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MCPServer

# JSON-RPC 2.0 Sec. 5.1 parse error - returned for a line that is not valid
# JSON. The id is null because the request could not be read.
_JSONRPC_PARSE_ERROR = -32700


class StdioTransport:
    """Drive an `MCPServer` over a line-delimited JSON byte stream."""

    __slots__ = ("server", "_read_line", "_write_line")

    def __init__(
        self,
        server: MCPServer,
        read_line: Callable[[], Awaitable[bytes | None]],
        write_line: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self.server = server
        self._read_line = read_line
        self._write_line = write_line

    async def serve(self) -> None:
        """Read, dispatch, and reply line-by-line until the input closes.

        A blank line is skipped; an unparseable line yields a JSON-RPC parse
        error; a notification (no response) writes nothing. The loop ends
        when `read_line` returns `None` (EOF).

        Framing is newline-delimited per the MCP stdio transport spec
        ("messages are delimited by newlines, and MUST NOT contain embedded
        newlines"). This is deliberate and correct: the MCP stdio transport
        does NOT use LSP-style `Content-Length:` header framing - that belongs
        to the Language Server Protocol, not MCP. One JSON line in, one JSON
        line out. Do not "fix" this into header framing.
        """
        while True:
            line = await self._read_line()
            if line is None:
                return
            stripped = line.strip()
            if not stripped:
                continue
            response = await self._dispatch_line(stripped)
            if response is not None:
                await self._write_line(orjson.dumps(response))

    async def _dispatch_line(self, line: bytes) -> dict[str, Any] | None:
        try:
            message = orjson.loads(line)
        except orjson.JSONDecodeError:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": _JSONRPC_PARSE_ERROR, "message": "Parse error"},
            }
        if not isinstance(message, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": _JSONRPC_PARSE_ERROR, "message": "Parse error"},
            }
        return await self.server.handle_message(message)


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
