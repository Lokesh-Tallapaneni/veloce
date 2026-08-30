"""Shared harnesses for the MCP test cluster.

Thirty-five modules cover `veloce.contrib.mcp` and none of them shared a
harness, so the two things nearly all of them need were copied instead. The
`_Pipe` stdio driver is byte-identical in three modules; the `MCPAuth` builder
is identical in four modules modulo its verifier, differing only in a URL that
does not matter to any of them.

These are the copies, once. What each module keeps is the part that is actually
about its own subject.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

import orjson

from veloce import Veloce
from veloce.contrib.mcp import MCPAuth, MCPServer
from veloce.contrib.mcp.transports.stdio import StdioTransport
from veloce.principal import Principal

RESOURCE_SERVER_URL = "https://api.example.com/mcp"
AUTHORIZATION_SERVER_URL = "https://auth.example.com"


def greeting_server(completer: Callable | None = None) -> MCPServer:
    """A server with a `greet(name)` prompt, plus a `name` completer if given.

    Ten tests in `test_mcp_completion.py` retyped this pair verbatim. What
    varies between them is the completer body, so that is the argument; the
    prompt is the same three lines every time and says nothing about any of
    them.

    Tests needing a different prompt - a second uncompleted argument, a
    resource template - build their own, because the shape *is* their subject.
    """
    app = Veloce()

    @app.mcp_prompt(description="A greeting")
    async def greet(name: str) -> str:
        return f"Hi {name}"

    if completer is not None:
        app.mcp_completer(prompt="greet", argument="name")(completer)
    return MCPServer(app)


class Pipe:
    """Drive a `StdioTransport` in-process: feed request lines, collect replies.

    `feed` queues a message; `run` serves until the inbox is empty and returns
    everything written back, already decoded.
    """

    def __init__(self, server: MCPServer) -> None:
        self._inbox: list[bytes] = []
        self.outbox: list[dict] = []
        self.transport = StdioTransport(server, self._read_line, self._write_line)

    def feed(self, message: dict) -> Pipe:
        self._inbox.append(orjson.dumps(message))
        return self

    async def _read_line(self) -> bytes | None:
        if not self._inbox:
            return None
        return self._inbox.pop(0)

    async def _write_line(self, data: bytes) -> None:
        self.outbox.append(orjson.loads(data))

    async def run(self) -> list[dict]:
        await self.transport.serve()
        return self.outbox


def auth(
    verify: Any = None,
    *,
    resource_server_url: str = RESOURCE_SERVER_URL,
    authorization_servers: list[str] | None = None,
) -> MCPAuth:
    """An `MCPAuth` with the cluster's standard URLs.

    The default `verify` accepts the token `"good"` and refuses everything else,
    which is what most of the copies wanted; pass your own when the verifier is
    the subject.
    """
    return MCPAuth(
        verify=verify if verify is not None else accepts_good,
        resource_server_url=resource_server_url,
        authorization_servers=authorization_servers or [AUTHORIZATION_SERVER_URL],
    )


def accepts_good(token: str) -> Principal | None:
    """Accept exactly the token `"good"`. The usual stand-in for a real verifier."""
    return Principal(subject="s") if token == "good" else None


def accepts_any(token: str) -> Principal:
    """Accept every token - for tests where admission is not the subject."""
    return Principal(subject=token or "anonymous")


class SSEStream:
    """An open `GET`, with its SSE frames readable one at a time.

    Two modules carried this class with the frame parser copied line for line,
    and neither was shaped as a fixture, so nine tests drove `__aenter__` /
    `__aexit__` by hand. Use it as an async context manager:

        async with SSEStream(app) as stream:
            assert (await stream.message())["result"]

    `event()` returns the next complete frame as a field mapping; `message()`
    returns the next JSON-RPC payload carried on one. Both take a timeout, so a
    stream that never produces fails as a timeout rather than hanging the suite.
    """

    def __init__(
        self,
        app: Any,
        path: str = "/sse",
        headers: list | None = None,
    ) -> None:
        self._app = app
        self._path = path
        self._headers = list(headers or []) + [(b"accept", b"text/event-stream")]
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._buffer = ""
        self.status: int | None = None
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> SSEStream:
        self.task = asyncio.ensure_future(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def _run(self) -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": self._path,
            "raw_path": self._path.encode(),
            "query_string": b"",
            "headers": self._headers,
            "client": ("127.0.0.1", 5555),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "root_path": "",
        }
        first = True

        async def receive() -> dict:
            # The body is drained before dispatch, so the empty body is delivered
            # first; after that the client simply stays connected.
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
            elif message["type"] == "http.response.body":
                await self._chunks.put(message.get("body", b""))

        await self._app(scope, receive, send)

    async def event(self, timeout: float = 5.0) -> dict[str, str]:
        """Return the next complete SSE frame as a field mapping."""
        while True:
            if "\n\n" in self._buffer:
                raw, _, self._buffer = self._buffer.partition("\n\n")
                fields = {}
                for line in raw.splitlines():
                    if line and ":" in line:
                        key, _, value = line.partition(":")
                        fields[key.strip()] = value.strip()
                if fields:
                    return fields
                continue
            self._buffer += (await asyncio.wait_for(self._chunks.get(), timeout)).decode()

    async def message(self, timeout: float = 5.0) -> dict:
        """Return the next JSON-RPC payload carried on the stream."""
        while True:
            frame = await self.event(timeout)
            if frame.get("event") == "message":
                return json.loads(frame["data"])

    async def wait_status(self, timeout: float = 5.0) -> int:
        """Return the response status once the stream has opened.

        A refused stream sends only `http.response.start`, so `event()` never
        returns and the status is the only thing to wait for. Tests used to
        sleep a fixed 0.05s and then read `self.status`, which is a guess in
        both directions: too long on a fast machine, and a flake on a loaded
        one.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.status is None:
            if loop.time() >= deadline:
                raise AssertionError("the stream never produced a response status")
            await asyncio.sleep(0)
        return self.status

    async def settled(self, turns: int = 50) -> None:
        """Give a cancelled stream's cleanup a chance to run.

        Yields to the loop rather than sleeping a fixed interval: the generator
        teardown a caller is waiting on is scheduled work, not elapsed time.
        """
        for _ in range(turns):
            await asyncio.sleep(0)


async def await_tasks(server: MCPServer) -> None:
    """Await every still-running task runner so the caller sees settled state.

    The deterministic alternative to polling `tasks/get` behind a bounded sleep
    loop: it finishes as soon as the runners do, and it cannot exhaust its
    budget on a loaded machine and assert on a status that is merely late.
    """
    for runner in [t.runner for t in server._tasks.tasks.values() if t.runner is not None]:
        with contextlib.suppress(asyncio.CancelledError):
            await runner


async def call(server: MCPServer, method: str, params: dict | None = None, *, id: int = 1) -> Any:
    """Dispatch one request through `handle_message` and return its `result`.

    Eleven modules called the private handler behind a method
    (`server._tools_call({...})`) to skip writing the JSON-RPC envelope. That is
    convenience, but it also skips the dispatch map, the in-flight tracking and
    the error shaping - so a method that was never registered, or registered
    under the wrong name, passed those tests and failed for a real client.

    Raises `AssertionError` carrying the JSON-RPC error when the call fails, so
    a test that expected success reports what went wrong rather than an opaque
    `KeyError: 'result'`.
    """
    envelope = await call_raw(server, method, params, id=id)
    assert envelope is not None, f"{method} returned no response"
    if "error" in envelope:
        raise AssertionError(f"{method} failed: {envelope['error']}")
    return envelope["result"]


async def call_raw(
    server: MCPServer, method: str, params: dict | None = None, *, id: int | None = 1
) -> dict | None:
    """Dispatch one request and return the whole JSON-RPC envelope.

    Pass `id=None` to send a notification, which has no response.
    """
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        message["id"] = id
    if params is not None:
        message["params"] = params
    return await server.handle_message(message)


async def call_error(server: MCPServer, method: str, params: dict | None = None) -> dict:
    """Dispatch one request expected to fail, and return its `error` object."""
    envelope = await call_raw(server, method, params)
    assert envelope is not None, f"{method} returned no response"
    assert "error" in envelope, f"{method} unexpectedly succeeded: {envelope}"
    return envelope["error"]
