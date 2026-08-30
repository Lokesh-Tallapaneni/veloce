"""The streamable HTTP transport, and isolation between its clients.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import asyncio
import contextlib

import orjson
import pytest

from tests._mcp import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RESOURCE_NOT_FOUND,
    Pipe,
)
from tests._mcp_shared import (
    _drive_stream,
    _mcp_call_body,
    _parse_sse,
    _server,
)
from veloce import (
    MCPContext,
    Veloce,
)
from veloce.contrib.mcp import MCPSession
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.tasks import STATUS_COMPLETED
from veloce.contrib.mcp.transports.event_store import SSEEventStore
from veloce.contrib.mcp.transports.http import _stream_response
from veloce.contrib.mcp.transports.session_store import HttpSessionStore

# -- Streamable HTTP transport ----------------------------------------


def test_http_tool_call_returns_json():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
        },
    )
    assert resp.status_code == 200
    body = orjson.loads(resp.body)
    assert body["result"]["content"][0]["text"] == "5"


def test_http_initialize_returns_capabilities():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", path="/rpc")
    resp = app.test_client().post(
        "/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    body = orjson.loads(resp.body)
    assert body["result"]["serverInfo"]["name"]
    assert "tools" in body["result"]["capabilities"]


def test_http_notification_returns_202():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    # A notification (no id) carries no reply.
    resp = app.test_client().post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert resp.status_code == 202
    assert resp.body == b""


def test_http_parse_error_is_400():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400
    assert orjson.loads(resp.body)["error"]["code"] == PARSE_ERROR


def test_http_unknown_method_is_json_error():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "does/not/exist", "params": {}}
    )
    body = orjson.loads(resp.body)
    assert body["error"]["code"] == METHOD_NOT_FOUND


def test_http_resource_read_over_http():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{user_id}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        return {"id": user_id}

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "users://9"},
        },
    )
    body = orjson.loads(resp.body)
    assert orjson.loads(body["result"]["contents"][0]["text"]) == {"id": 9}


def test_http_sse_streams_progress_then_response():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work with progress")
    async def work(ctx: MCPContext) -> str:
        await ctx.report_progress(1, 2)
        await ctx.report_progress(2, 2)
        return "done"

    app.mount_mcp(transport="http")
    resp = app.test_client().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "work", "arguments": {}, "_meta": {"progressToken": "p1"}},
        },
        headers={"accept": "text/event-stream"},
    )
    assert "text/event-stream" in resp.content_type
    events = _parse_sse(resp.body)
    progresses = [e for e in events if e.get("method") == "notifications/progress"]
    assert len(progresses) == 2
    assert progresses[0]["params"]["progressToken"] == "p1"
    # The final SSE event is the JSON-RPC response, after the progress events.
    response = events[-1]
    assert response["id"] == 1
    assert response["result"]["content"][0]["text"] == "done"


def test_http_sse_concurrent_calls_do_not_cross_notifications():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo a label as progress")
    async def echo(label: str, ctx: MCPContext) -> str:
        await ctx.report_progress(1, 1, message=label)
        return label

    app.mount_mcp(transport="http")
    client = app.test_client()

    def call(label: str) -> list[dict]:
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "echo",
                    "arguments": {"label": label},
                    "_meta": {"progressToken": label},
                },
            },
            headers={"accept": "text/event-stream"},
        )
        return _parse_sse(resp.body)

    # Each call's progress notification carries only its own token, never the
    # other call's - the per-request notifier scoping holds.
    a_events = call("a")
    b_events = call("b")
    a_tokens = {e["params"]["progressToken"] for e in a_events if e.get("method")}
    b_tokens = {e["params"]["progressToken"] for e in b_events if e.get("method")}
    assert a_tokens == {"a"}
    assert b_tokens == {"b"}


# -- HTTP per-connection isolation (multi-client) ---------------------


async def test_http_cancel_does_not_cross_connections():
    """One HTTP client's cancel must not cancel a peer's call with a colliding id."""

    app = Veloce(openapi_url=None)
    released = {"a": asyncio.Event(), "b": asyncio.Event()}
    completed = {"a": asyncio.Event(), "b": asyncio.Event()}
    started = {"a": asyncio.Event(), "b": asyncio.Event()}

    @app.mcp_tool(description="Block until released")
    async def work(which: str) -> str:
        started[which].set()
        await released[which].wait()
        completed[which].set()
        return which

    server = MCPServer(app)
    # Two distinct connections sharing the one server, each issuing id 1.
    session_a = MCPSession()
    session_b = MCPSession()
    stream_a = _stream_response(server, _mcp_call_body("work", {"which": "a"}), None, session_a)
    stream_b = _stream_response(server, _mcp_call_body("work", {"which": "b"}), None, session_b)
    gen_a, gen_b = stream_a._stream, stream_b._stream
    await gen_a.__anext__()
    await gen_b.__anext__()
    await asyncio.wait_for(started["a"].wait(), timeout=1)
    await asyncio.wait_for(started["b"].wait(), timeout=1)

    # Connection A cancels its own id 1; B's id 1 must be untouched.
    await server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        session_a,
    )
    await _drive_stream(stream_a)
    # B finishes normally once released; A never reached completion.
    released["b"].set()
    await asyncio.wait_for(completed["b"].wait(), timeout=1)
    await _drive_stream(stream_b)
    released["a"].set()
    await asyncio.sleep(0)

    assert not completed["a"].is_set()
    assert completed["b"].is_set()
    assert server._inflight == {}


async def test_http_subscriptions_advertised_and_served_with_sessions():
    """Subscriptions advertise true and deliver updates over a stateful HTTP stream."""

    app = Veloce(openapi_url=None)
    app.config["MCP_RESOURCE_SUBSCRIPTIONS"] = True
    started = asyncio.Event()
    release = asyncio.Event()

    @app.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="doc://main",
        mcp_description="A document",
    )
    async def doc() -> dict:
        return {"v": 1}

    @app.mcp_tool(description="Hold the stream open while a change is signalled")
    async def hold() -> str:
        started.set()
        await release.wait()
        return "ok"

    server = MCPServer(app)
    session = MCPSession()

    # A stateful connection sees subscribe/listChanged advertised as true.
    init = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, session
    )
    assert init["result"]["capabilities"]["resources"] == {
        "subscribe": True,
        "listChanged": True,
    }

    # Subscribe, then open a stream that registers the connection's sink and, while
    # the call is held in flight, signal a change; the update reaches the stream.
    await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/subscribe",
            "params": {"uri": "doc://main"},
        },
        session,
    )

    stream = _stream_response(server, _mcp_call_body("hold", {}), None, session)
    gen = stream._stream
    await gen.__anext__()  # priming frame; registers the connection's sink
    await asyncio.wait_for(started.wait(), timeout=1)
    await server.notify_resource_updated("doc://main")
    release.set()
    frames = b"".join([chunk async for chunk in gen])
    methods = [e.get("method") for e in _parse_sse(frames)]
    assert "notifications/resources/updated" in methods


def test_http_subscribe_not_advertised_without_sessions():
    """Stateless HTTP must advertise subscribe/listChanged as false (cannot serve them)."""
    app = Veloce(openapi_url=None)
    app.config["MCP_RESOURCE_SUBSCRIPTIONS"] = True

    @app.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="doc://main",
        mcp_description="A document",
    )
    async def doc() -> dict:
        return {"v": 1}

    app.mount_mcp(transport="http")  # stateless: no sessions
    resp = app.test_client().post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    caps = orjson.loads(resp.body)["result"]["capabilities"]
    assert caps["resources"] == {"subscribe": False, "listChanged": False}


def test_http_subscribe_advertised_true_with_sessions():
    """With sessions on, the HTTP initialize advertises subscribe/listChanged true."""
    app = Veloce(openapi_url=None)
    app.config["MCP_RESOURCE_SUBSCRIPTIONS"] = True

    @app.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="doc://main",
        mcp_description="A document",
    )
    async def doc() -> dict:
        return {"v": 1}

    app.mount_mcp(transport="http", sessions=True)
    resp = app.test_client().post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    caps = orjson.loads(resp.body)["result"]["capabilities"]
    assert caps["resources"] == {"subscribe": True, "listChanged": True}


def test_http_subscribe_rejected_on_stateless_request():
    """resources/subscribe over a stateless HTTP request errors (not advertised)."""
    app = Veloce(openapi_url=None)
    app.config["MCP_RESOURCE_SUBSCRIPTIONS"] = True

    @app.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="doc://main",
        mcp_description="A document",
    )
    async def doc() -> dict:
        return {"v": 1}

    app.mount_mcp(transport="http")  # stateless
    resp = app.test_client().post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/subscribe",
            "params": {"uri": "doc://main"},
        },
    )
    assert orjson.loads(resp.body)["error"]["code"] == INVALID_PARAMS


def test_http_tasks_isolated_per_session():
    """A task created under one Mcp-Session-Id is invisible to another session."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Slow", task_support=True)
    async def slow() -> str:
        await asyncio.sleep(0.2)
        return "done"

    app.mount_mcp(transport="http", sessions=True)
    client = app.test_client()

    def initialize() -> str:
        init = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        return init.headers["mcp-session-id"]

    sid_a = initialize()
    sid_b = initialize()

    created = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {}, "task": {}},
        },
        headers={"mcp-session-id": sid_a},
    )
    task_id = orjson.loads(created.body)["result"]["task"]["taskId"]

    # Client B cannot see A's task in its own list.
    listed_b = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tasks/list", "params": {}},
        headers={"mcp-session-id": sid_b},
    )
    assert orjson.loads(listed_b.body)["result"]["tasks"] == []

    # Client B cannot get / cancel A's task even with the id.
    got_b = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 4, "method": "tasks/get", "params": {"taskId": task_id}},
        headers={"mcp-session-id": sid_b},
    )
    assert orjson.loads(got_b.body)["error"]["code"] == RESOURCE_NOT_FOUND

    cancel_b = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 5, "method": "tasks/cancel", "params": {"taskId": task_id}},
        headers={"mcp-session-id": sid_b},
    )
    assert orjson.loads(cancel_b.body)["error"]["code"] == RESOURCE_NOT_FOUND

    # Client A still sees and owns its task.
    listed_a = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 6, "method": "tasks/list", "params": {}},
        headers={"mcp-session-id": sid_a},
    )
    a_ids = [t["taskId"] for t in orjson.loads(listed_a.body)["result"]["tasks"]]
    assert task_id in a_ids


def test_http_task_support_requires_sessions():
    """Mounting HTTP with a task_support tool and no sessions raises a clear error."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Slow", task_support=True)
    async def slow() -> str:
        return "done"

    with pytest.raises(ValueError, match="requires sessions=True"):
        app.mount_mcp(transport="http")


def test_http_task_support_without_task_tools_allows_stateless():
    """HTTP without sessions mounts fine when no tool opts into task support."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Quick")
    async def quick() -> str:
        return "done"

    # No task_support tool, so the stateless default is valid and does not raise.
    assert app.mount_mcp(transport="http") is None


def test_http_client_capabilities_recorded_with_sessions():
    """initialize capabilities are recorded on the HTTP session, gating ctx.sample."""
    app = Veloce(openapi_url=None)

    app.debug = True

    @app.mcp_tool(description="Try sampling")
    async def sample_tool(ctx: MCPContext) -> str:
        await ctx.sample([{"role": "user", "content": "hi"}], max_tokens=16)
        return "ok"

    app.mount_mcp(transport="http", sessions=True)
    client = app.test_client()
    sid = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"sampling": {}}},
        },
    ).headers["mcp-session-id"]

    # The capability is recorded, so the call passes the capability gate and then
    # fails on the genuine transport limit (HTTP POST has no server->client reply
    # channel), not on a missing-capability error - proving the session recorded it.
    resp = client.post(
        "/mcp",
        json=_mcp_call_body("sample_tool", {}),
        headers={"mcp-session-id": sid},
    )
    body = orjson.loads(resp.body)
    # Not a top-level JSON-RPC error: a missing-capability gate would have produced
    # one. Instead the call passed the gate and failed in-band on the genuine
    # transport limit (a POST has no server->client reply channel).
    assert "error" not in body
    result = body["result"]
    assert result["isError"] is True
    assert "bidirectional" in result["content"][0]["text"]


def test_http_lifecycle_gating_enforced_with_sessions():
    """MCP_ENFORCE_LIFECYCLE rejects a pre-initialize call on a stateful HTTP session."""
    app = Veloce(openapi_url=None)
    app.config["MCP_ENFORCE_LIFECYCLE"] = True

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", sessions=True)
    client = app.test_client()
    sid = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    ).headers["mcp-session-id"]

    # A call before notifications/initialized is rejected.
    early = client.post(
        "/mcp", json=_mcp_call_body("add", {"a": 1, "b": 2}), headers={"mcp-session-id": sid}
    )
    assert orjson.loads(early.body)["error"]["code"] == INVALID_REQUEST

    # After the initialized ack, the same call succeeds.
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"mcp-session-id": sid},
    )
    ok = client.post(
        "/mcp", json=_mcp_call_body("add", {"a": 1, "b": 2}), headers={"mcp-session-id": sid}
    )
    assert orjson.loads(ok.body)["result"]["content"][0]["text"] == "3"


async def test_http_session_store_evicts_idle_sessions():
    """An idle, never-DELETEd session is reclaimed by the store's idle TTL."""

    store = HttpSessionStore(idle_ttl=0.0)
    sid, _session = await store.create()
    # With a zero idle window any subsequent resolution evicts the stale id.
    assert await store.resolve(sid) is None


def test_event_store_caps_stream_count():
    """The event store evicts the oldest stream once the stream cap is exceeded."""

    store = SSEEventStore(max_streams=2)
    store.record("s1", 1, {"v": 1})
    store.record("s2", 1, {"v": 2})
    store.record("s3", 1, {"v": 3})  # evicts s1 (oldest)
    assert store.replay_after("s1.0") == []  # s1 history reclaimed
    assert [p["v"] for _, p in store.replay_after("s3.0")] == [3]
    assert [p["v"] for _, p in store.replay_after("s2.0")] == [2]


def test_session_connection_ids_are_unique_and_monotonic():
    """Each MCPSession gets a process-unique, never-recycled connection id."""
    ids = [MCPSession().connection_id for _ in range(5)]
    assert len(set(ids)) == 5
    assert ids == sorted(ids)


async def test_evict_session_reclaims_never_settling_task():
    """An owning session's eviction cancels and drops its non-terminal task."""
    app = Veloce(openapi_url=None)
    started = asyncio.Event()

    @app.mcp_tool(description="Never settles", task_support=True)
    async def stuck() -> str:
        started.set()
        await asyncio.Event().wait()  # never completes on its own
        return "unreachable"

    server = MCPServer(app)
    session = MCPSession()
    created = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "stuck", "arguments": {}, "task": {}},
        },
        session,
    )
    task_id = created["result"]["task"]["taskId"]
    await asyncio.wait_for(started.wait(), timeout=1)
    task = server._tasks.get(task_id)
    assert task is not None and not task.is_terminal()
    runner = task.runner

    # Evicting the session reclaims its task: the runner is cancelled and the
    # task is dropped from the store, so a stuck handler cannot pin memory.
    server.evict_session(session)
    assert server._tasks.get(task_id) is None
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert runner.cancelled()


async def test_task_ownership_survives_session_id_recycle():
    """A task owner_key keys off the stable connection id, not id(session).

    A later session cannot inherit an earlier session's tasks even if CPython
    recycles the freed session's memory address.
    """
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Quick", task_support=True)
    async def quick() -> str:
        return "ok"

    server = MCPServer(app)
    session_a = MCPSession()
    created = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "quick", "arguments": {}, "task": {}},
        },
        session_a,
    )
    task_id = created["result"]["task"]["taskId"]
    await asyncio.sleep(0)

    # A second session - even one CPython may give the freed address of the first -
    # owns a distinct connection id, so A's task is invisible and inaccessible.
    session_b = MCPSession()
    assert session_b.connection_id != session_a.connection_id
    got = await server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"taskId": task_id}},
        session_b,
    )
    assert got["error"]["code"] == RESOURCE_NOT_FOUND
    listed = await server.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "tasks/list", "params": {}},
        session_b,
    )
    assert listed["result"]["tasks"] == []


async def test_stdio_task_augmented_tool_can_sample():
    """A task-augmented stdio tool may issue a server->client request.

    It could not while the serve loop and the calling handler were two readers
    of one stream: `request` pumped stdin itself, so a task - which runs after
    the CreateTaskResult has already been returned - would have raced the loop.
    The loop is now the sole reader and settles the future, so there is nothing
    left to refuse.
    """

    app = Veloce(openapi_url=None)
    app.debug = True

    @app.mcp_tool(description="Samples from a task", task_support=True)
    async def sampler(ctx: MCPContext) -> str:
        reply = await ctx.sample([{"role": "user", "content": "hi"}], max_tokens=8)
        return reply.get("content", {}).get("text", "")

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"sampling": {}}},
        }
    )
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "sampler", "arguments": {}, "task": {}},
        }
    )

    # Answer the server's sampling request as a real client would: watch for it
    # on the outbound side and feed the correlated reply back in.
    server = pipe.transport.server
    captured: list = []
    read_line = pipe.transport._read_line
    answered = [False]

    async def _answer_then_eof():
        line = await read_line()
        if line is not None:
            return line
        if not answered[0]:
            for message in pipe.outbox:
                if message.get("method") == "sampling/createMessage":
                    answered[0] = True
                    return orjson.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "result": {"content": {"type": "text", "text": "sampled"}},
                        }
                    )
            await asyncio.sleep(0)
            return b""
        for pending in list(server._tasks.tasks.values()):
            if pending.runner is not None:
                with contextlib.suppress(Exception):
                    await pending.runner
        captured.extend(server._tasks.tasks.values())
        return None

    pipe.transport._read_line = _answer_then_eof
    out = await pipe.run()

    task_id = next(
        message["result"]["task"]["taskId"]
        for message in out
        if isinstance(message.get("result"), dict) and "task" in message["result"]
    )
    task = next(t for t in captured if t.name == task_id)
    assert task.status == STATUS_COMPLETED, task.status_message
    assert task.result["content"][0]["text"] == "sampled"
