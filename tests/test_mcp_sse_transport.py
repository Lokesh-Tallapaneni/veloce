"""The legacy split-endpoint SSE transport (MCP revision 2024-11-05).

Two endpoints instead of one: a long-lived `GET` carrying everything the server
says, and a `POST` carrying everything the client says. The client learns the
POST URL from the stream — the first frame is an `endpoint` event naming it,
with the session id that ties the halves together.

The asymmetry is the point: a `POST` is answered `202` with no body and its
JSON-RPC response arrives later on the stream. These tests drive the ASGI app
directly, because the stream never ends and the test client has no incremental
reader.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import pytest

from veloce import AsyncTestClient, MCPContext, Veloce


class _Stream:
    """An open `GET`, with its SSE frames readable one at a time."""

    def __init__(self, app: Veloce, path: str = "/sse", headers: list | None = None) -> None:
        self._app = app
        self._path = path
        self._headers = headers or [(b"accept", b"text/event-stream")]
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._buffer = ""
        self.status: int | None = None
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> _Stream:
        self.task = asyncio.ensure_future(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.task is not None:
            self.task.cancel()

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


def _app(**mount: object) -> Veloce:
    app = Veloce(title="LegacySSE", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    @app.mcp_tool(description="Report progress then finish")
    async def walk(steps: int, ctx: MCPContext) -> dict:
        for index in range(steps):
            await ctx.report_progress(index + 1, steps)
        return {"steps": steps}

    app.mount_mcp(transport="sse", **mount)
    return app


def _session_of(endpoint: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(endpoint).query)["sessionId"][0]


def _call(ident: int, name: str, arguments: dict, meta: dict | None = None) -> dict:
    params: dict = {"name": name, "arguments": arguments}
    if meta is not None:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": params}


# ── The endpoint handshake ───────────────────────────────────────────


async def test_the_first_frame_names_the_url_to_post_to():
    """A client cannot speak until it has this, so it is sent before anything else."""
    app = _app()
    async with _Stream(app) as stream:
        frame = await stream.event()
        assert frame["event"] == "endpoint"
        assert frame["data"].startswith("/messages?sessionId=")


async def test_the_endpoint_event_reflects_a_custom_message_path():
    app = _app(path="/agent/sse", message_path="/agent/messages")
    async with _Stream(app, path="/agent/sse") as stream:
        frame = await stream.event()
        assert frame["data"].startswith("/agent/messages?sessionId=")


async def test_each_stream_gets_its_own_session():
    app = _app()
    async with _Stream(app) as first, _Stream(app) as second:
        one = _session_of((await first.event())["data"])
        two = _session_of((await second.event())["data"])
        assert one != two


async def test_a_session_id_is_not_guessable():
    """It is the only thing tying a POST to the stream that will answer it."""
    app = _app()
    async with _Stream(app) as stream:
        session_id = _session_of((await stream.event())["data"])
        assert len(session_id) >= 32


# ── A message, and its answer on the stream ──────────────────────────


async def test_a_post_is_acknowledged_without_the_answer():
    app = _app()
    async with _Stream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            response = await client.post(endpoint, json=_call(1, "add", {"a": 2, "b": 3}))
        assert response.status_code == 202
        assert response.body == b""


async def test_the_answer_arrives_on_the_stream():
    app = _app()
    async with _Stream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            await client.post(endpoint, json=_call(1, "add", {"a": 2, "b": 3}))
        payload = await stream.message()
        assert payload["id"] == 1
        assert payload["result"]["content"][0]["text"] == "5"


async def test_two_requests_are_answered_in_order():
    app = _app()
    async with _Stream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            await client.post(endpoint, json=_call(1, "add", {"a": 1, "b": 1}))
            first = await stream.message()
            await client.post(endpoint, json=_call(2, "add", {"a": 2, "b": 2}))
            second = await stream.message()
        assert [first["id"], second["id"]] == [1, 2]
        assert second["result"]["content"][0]["text"] == "4"


async def test_a_notification_is_answered_with_nothing():
    """JSON-RPC: a message with no id has no response, so no frame is sent."""
    app = _app()
    async with _Stream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            posted = await client.post(
                endpoint, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            assert posted.status_code == 202
            await client.post(endpoint, json=_call(7, "add", {"a": 1, "b": 1}))
        # The next payload is the call's answer, not anything for the notification.
        assert (await stream.message())["id"] == 7


async def test_a_progress_notification_precedes_the_response():
    """Both travel the same channel, so the client sees them in the order produced."""
    app = _app()
    async with _Stream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            await client.post(
                endpoint,
                json=_call(1, "walk", {"steps": 2}, meta={"progressToken": "tok"}),
            )
        first = await stream.message()
        assert first["method"] == "notifications/progress"
        while "id" not in (payload := await stream.message()):
            continue
        assert payload["id"] == 1


async def test_one_stream_does_not_receive_another_stream_answer():
    app = _app()
    async with _Stream(app) as first, _Stream(app) as second:
        first_endpoint = (await first.event())["data"]
        await second.event()
        async with AsyncTestClient(app) as client:
            await client.post(first_endpoint, json=_call(1, "add", {"a": 4, "b": 4}))
        assert (await first.message())["result"]["content"][0]["text"] == "8"
        with pytest.raises(asyncio.TimeoutError):
            await second.event(timeout=0.3)


# ── A POST that names no live stream ─────────────────────────────────


async def test_a_post_without_a_session_is_refused():
    app = _app()
    async with AsyncTestClient(app) as client:
        response = await client.post("/messages", json=_call(1, "add", {"a": 1, "b": 1}))
    assert response.status_code == 400


async def test_a_post_naming_an_unknown_session_is_not_found():
    app = _app()
    async with AsyncTestClient(app) as client:
        response = await client.post(
            "/messages?sessionId=never-issued", json=_call(1, "add", {"a": 1, "b": 1})
        )
    assert response.status_code == 404


async def test_a_body_that_is_not_a_json_rpc_object_is_refused():
    """There is no stream frame to carry an error for a message with no readable id."""
    app = _app()
    async with _Stream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            response = await client.post(
                endpoint, content=b"not json", headers={"content-type": "application/json"}
            )
        assert response.status_code == 400


async def test_a_closed_stream_stops_accepting_its_session():
    app = _app()
    async with _Stream(app) as stream:
        endpoint = (await stream.event())["data"]
    # Let the generator's cleanup run now the stream is cancelled.
    await asyncio.sleep(0.05)
    async with AsyncTestClient(app) as client:
        response = await client.post(endpoint, json=_call(1, "add", {"a": 1, "b": 1}))
    assert response.status_code == 404


# ── Mounting ─────────────────────────────────────────────────────────


def test_mounting_registers_both_halves():
    app = _app()
    registered = {
        (method, path) for method, path, _ in app._collect_all_routes(include_hidden=True)
    }
    assert ("GET", "/sse") in registered
    assert ("POST", "/messages") in registered


def test_the_stream_path_defaults_to_sse_not_the_http_default():
    """`/mcp` is the Streamable HTTP default; this transport's own default is `/sse`."""
    app = Veloce(title="Defaults", openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="sse")
    registered = {path for _method, path, _ in app._collect_all_routes(include_hidden=True)}
    assert "/sse" in registered


def test_an_unknown_transport_still_reports_what_is_supported():
    app = Veloce(title="Unknown", openapi_url=None)
    with pytest.raises(ValueError, match="'stdio', 'http', and 'sse'"):
        app.mount_mcp(transport="carrier-pigeon")


# ── The same protections the other HTTP transport has ────────────────


async def test_a_disallowed_origin_cannot_open_a_stream():
    """DNS-rebinding defense: a browser page on another origin is refused."""
    app = _app(allowed_origins=["https://app.example.com"])
    async with _Stream(
        app,
        headers=[(b"accept", b"text/event-stream"), (b"origin", b"https://evil.example")],
    ) as stream:
        await asyncio.sleep(0.05)
        assert stream.status == 403


async def test_an_allowed_origin_opens_a_stream():
    app = _app(allowed_origins=["https://app.example.com"])
    async with _Stream(
        app,
        headers=[(b"accept", b"text/event-stream"), (b"origin", b"https://app.example.com")],
    ) as stream:
        assert (await stream.event())["event"] == "endpoint"


async def test_a_disallowed_origin_cannot_post():
    app = _app(allowed_origins=["https://app.example.com"])
    async with AsyncTestClient(app) as client:
        response = await client.post(
            "/messages?sessionId=x",
            json=_call(1, "add", {"a": 1, "b": 1}),
            headers={"origin": "https://evil.example"},
        )
    assert response.status_code == 403


async def test_an_unauthenticated_stream_is_challenged():
    from veloce.contrib.mcp import MCPAuth

    app = _app(
        auth=MCPAuth(
            verify=lambda token: None,
            resource_server_url="https://api.example.com/sse",
            authorization_servers=["https://auth.example.com"],
        )
    )
    async with _Stream(app) as stream:
        await asyncio.sleep(0.05)
        assert stream.status == 401


async def test_an_unauthenticated_post_is_challenged():
    from veloce.contrib.mcp import MCPAuth

    app = _app(
        auth=MCPAuth(
            verify=lambda token: None,
            resource_server_url="https://api.example.com/sse",
            authorization_servers=["https://auth.example.com"],
        )
    )
    async with AsyncTestClient(app) as client:
        response = await client.post("/messages?sessionId=x", json=_call(1, "add", {"a": 1}))
    assert response.status_code == 401


async def test_an_authenticated_client_is_served():
    from veloce.contrib.mcp import MCPAuth
    from veloce.principal import Principal

    app = _app(
        auth=MCPAuth(
            verify=lambda token: Principal(subject="agent") if token == "ok" else None,
            resource_server_url="https://api.example.com/sse",
            authorization_servers=["https://auth.example.com"],
        )
    )
    async with _Stream(
        app,
        headers=[(b"accept", b"text/event-stream"), (b"authorization", b"Bearer ok")],
    ) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            posted = await client.post(
                endpoint,
                json=_call(1, "add", {"a": 2, "b": 3}),
                headers={"authorization": "Bearer ok"},
            )
        assert posted.status_code == 202
        assert (await stream.message())["result"]["content"][0]["text"] == "5"
