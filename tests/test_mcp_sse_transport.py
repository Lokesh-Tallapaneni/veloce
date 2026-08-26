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
import urllib.parse

import pytest

from tests._mcp import SSEStream
from veloce import AsyncTestClient, MCPContext, Veloce


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
    async with SSEStream(app) as stream:
        frame = await stream.event()
        assert frame["event"] == "endpoint"
        assert frame["data"].startswith("/messages?sessionId=")


async def test_the_endpoint_event_reflects_a_custom_message_path():
    app = _app(path="/agent/sse", message_path="/agent/messages")
    async with SSEStream(app, path="/agent/sse") as stream:
        frame = await stream.event()
        assert frame["data"].startswith("/agent/messages?sessionId=")


# ── The reconnect hint ───────────────────────────────────────────────
#
# The stream carried a closing `retry` frame after its loop, but nothing ever
# queued the sentinel whose `break` was the only way to reach it - so on this
# transport the hint was never sent and a dropped client fell back to its own
# default. This stream only ends once the client is already gone, so there is
# no point at which a closing frame could be delivered; WHATWG SSE applies
# `retry` as soon as it is parsed, so it rides the first frame instead.


async def test_the_first_frame_carries_the_reconnect_hint():
    """The defect: no frame on this transport carried `retry` at all."""
    app = _app()
    async with SSEStream(app) as stream:
        frame = await stream.event()
        assert frame["retry"] == "3000"


async def test_the_reconnect_hint_rides_the_endpoint_frame():
    """One frame, so a client has both before it can do anything."""
    app = _app()
    async with SSEStream(app) as stream:
        frame = await stream.event()
        assert frame["event"] == "endpoint"
        assert "retry" in frame
        assert frame["data"].startswith("/messages?sessionId=")


async def test_the_hint_is_sent_before_any_message():
    """A hint that arrived after the first tool result would be too late for a
    client that dropped during the call."""
    app = _app()
    async with SSEStream(app) as stream:
        first = await stream.event()
        endpoint = first["data"]
        assert "retry" in first
        async with AsyncTestClient(app) as client:
            await client.post(endpoint, json=_call(1, "add", {"a": 1, "b": 2}))
            assert (await stream.message())["id"] == 1


async def test_later_frames_do_not_repeat_the_hint():
    """`retry` is sticky per WHATWG SSE, so repeating it on every frame would be
    bytes on the wire for nothing."""
    app = _app()
    async with SSEStream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            await client.post(endpoint, json=_call(1, "add", {"a": 1, "b": 2}))
            frame = await stream.event()
            while frame.get("event") != "message":
                frame = await stream.event()
            assert "retry" not in frame


async def test_each_stream_gets_its_own_session():
    app = _app()
    async with SSEStream(app) as first, SSEStream(app) as second:
        one = _session_of((await first.event())["data"])
        two = _session_of((await second.event())["data"])
        assert one != two


async def test_a_session_id_is_not_guessable():
    """It is the only thing tying a POST to the stream that will answer it."""
    app = _app()
    async with SSEStream(app) as stream:
        session_id = _session_of((await stream.event())["data"])
        assert len(session_id) >= 32


# ── A message, and its answer on the stream ──────────────────────────


async def test_a_post_is_acknowledged_without_the_answer():
    app = _app()
    async with SSEStream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            response = await client.post(endpoint, json=_call(1, "add", {"a": 2, "b": 3}))
        assert response.status_code == 202
        assert response.body == b""


async def test_the_answer_arrives_on_the_stream():
    app = _app()
    async with SSEStream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            await client.post(endpoint, json=_call(1, "add", {"a": 2, "b": 3}))
        payload = await stream.message()
        assert payload["id"] == 1
        assert payload["result"]["content"][0]["text"] == "5"


async def test_two_requests_are_answered_in_order():
    app = _app()
    async with SSEStream(app) as stream:
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
    async with SSEStream(app) as stream:
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
    async with SSEStream(app) as stream:
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
    async with SSEStream(app) as first, SSEStream(app) as second:
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


async def _refuse(body: bytes) -> dict:
    """POST `body` on a live session and return the JSON-RPC error it draws."""
    app = _app()
    async with SSEStream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            response = await client.post(
                endpoint, content=body, headers={"content-type": "application/json"}
            )
        assert response.status_code == 400
        return response.json()["error"]


async def test_a_body_that_is_not_a_json_rpc_object_is_refused():
    """There is no stream frame to carry an error for a message with no readable id."""
    error = await _refuse(b"not json")
    assert error["code"] == -32700


async def test_an_unreadable_body_is_a_parse_error():
    """JSON-RPC Sec. 5.1: -32700 says the text could not be read.

    This transport answered -32603 for both failures, so a client with per-code
    retry logic behaved differently purely by which wire it had connected over.
    """
    assert (await _refuse(b'{"jsonrpc": '))["code"] == -32700


async def test_a_readable_body_of_the_wrong_shape_is_an_invalid_request():
    """-32600 says what was read is not a Request object. A batch array lands here."""
    assert (await _refuse(b"[1,2,3]"))["code"] == -32600


@pytest.mark.parametrize(
    ("body", "code"), [(b'{"jsonrpc": ', -32700), (b"[1,2,3]", -32600), (b'"text"', -32600)]
)
async def test_this_transport_answers_what_the_others_answer(body, code):
    """One malformed body, one code, whichever transport received it."""
    assert (await _refuse(body))["code"] == code


async def test_a_closed_stream_stops_accepting_its_session():
    app = _app()
    async with SSEStream(app) as stream:
        endpoint = (await stream.event())["data"]
    # Let the generator's cleanup run now the stream is cancelled.
    await stream.settled()
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
    async with SSEStream(
        app,
        headers=[(b"accept", b"text/event-stream"), (b"origin", b"https://evil.example")],
    ) as stream:
        assert await stream.wait_status() == 403


async def test_an_allowed_origin_opens_a_stream():
    app = _app(allowed_origins=["https://app.example.com"])
    async with SSEStream(
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
    async with SSEStream(app) as stream:
        assert await stream.wait_status() == 401


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
    async with SSEStream(
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


# ── A closed stream reclaims what its session owned ──────────────────
#
# stdio evicts its session on EOF and the Streamable HTTP store evicts on
# expiry; this transport unregistered the connection and stopped there. That
# drops the notification sink and the listen streams but leaves the session's
# tasks registered, and `TaskRegistry.evict_expired` deliberately never reaps an
# unsettled task - so a never-settling task created on a stream outlived it,
# together with its running asyncio runner, for the lifetime of the process.


def _task_app() -> tuple[Veloce, object]:
    """An app whose SSE transport is registered explicitly, so the server is reachable."""
    from veloce.contrib.mcp.server import MCPServer
    from veloce.contrib.mcp.transports.sse import register_sse_transport

    app = Veloce(title="LegacySSE", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Never settles", task_support=True)
    async def blocker(ctx: MCPContext) -> int:
        await asyncio.Event().wait()
        return 1

    server = MCPServer(app)
    register_sse_transport(app, server)
    return app, server


async def test_a_closed_stream_reclaims_its_unsettled_tasks():
    """The defect: the task and its runner outlived the connection forever."""
    app, server = _task_app()
    async with SSEStream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            await client.post(
                endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "blocker", "arguments": {}, "task": {}},
                },
            )
        await stream.message()
        assert len(server._tasks.tasks) == 1, "the task should exist while the stream is open"

    # Let the generator's cleanup run now the stream is cancelled.
    await stream.settled()
    assert server._tasks.tasks == {}, "the task outlived the stream that owned it"


async def test_a_closed_stream_leaves_no_running_runner():
    """The registry entry is half of it; the asyncio task is the other half."""
    app, server = _task_app()
    async with SSEStream(app) as stream:
        endpoint = (await stream.event())["data"]
        async with AsyncTestClient(app) as client:
            await client.post(
                endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "blocker", "arguments": {}, "task": {}},
                },
            )
        await stream.message()
        runners = [t.runner for t in server._tasks.tasks.values() if t.runner is not None]
        assert runners and not all(r.done() for r in runners)

    await stream.settled()
    assert all(r.done() or r.cancelled() for r in runners)
