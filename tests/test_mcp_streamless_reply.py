"""A tool call is answered as a stream only when it actually produces one.

The spec lets a POST carrying a request be answered either as JSON or as an SSE
stream, and tells clients to accept both - so choosing on the `Accept` header
alone means a conformant client pays for stream framing on every call,
including one that emits exactly its own reply. Measured, that framing is a
third of the cost of a plain tool call.

What decides it is whether the call actually produces a second message, found
out by running it and watching what arrives first - not by guessing from the
method or the tool's registration. Only a few cases are forced to keep the
stream regardless of what this one call does: resumability (the stream mints
the event ids a replay needs), an open subscription connection (the only
delivery window for an out-of-band push), a task-augmented call (whose
`notifications/tasks/status` is defined to follow the reply), and a client
that refuses JSON outright.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests._mcp import initialize
from veloce import AsyncTestClient, Depends, Veloce
from veloce._handler_plan import build_plan
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.registry import MCPTool
from veloce.contrib.mcp.toolsearch import _meta_tool
from veloce.contrib.mcp.transports import http as mcp_http
from veloce.testclient import TestClient

INITIALIZE = initialize()
BOTH = "application/json, text/event-stream"


async def _ctx_dependency(ctx: MCPContext) -> int:
    return 1


def _app() -> Veloce:
    app = Veloce(title="Streamless", version="1.0.0", openapi_url=None)

    @app.get("/plain", expose_as_mcp_tool=True, mcp_description="Cannot reach the context")
    async def plain(x: int = 1) -> dict:
        return {"x": x}

    @app.get("/ctx", expose_as_mcp_tool=True, mcp_description="Takes the context, sends nothing")
    async def takes_ctx(ctx: MCPContext, x: int = 1) -> dict:
        return {"x": x}

    @app.get("/depctx", expose_as_mcp_tool=True, mcp_description="A dependency takes it")
    async def dep_ctx(d: int = Depends(_ctx_dependency), x: int = 1) -> dict:
        return {"x": x}

    @app.get("/notifies", expose_as_mcp_tool=True, mcp_description="Logs before replying")
    async def notifies(ctx: MCPContext, x: int = 1) -> dict:
        await ctx.info("working")
        return {"x": x}

    @app.get("/slow", expose_as_mcp_tool=True, mcp_description="Replies late, emits nothing")
    async def slow(ctx: MCPContext, x: int = 1) -> dict:
        await asyncio.sleep(0.2)
        return {"x": x}

    app.mount_mcp(transport="http", path="/mcp")
    return app


@pytest.fixture
def client() -> TestClient:
    c = TestClient(_app())
    response = c.post("/mcp", json=INITIALIZE, headers={"Accept": BOTH})
    session = response.headers.get("mcp-session-id")
    c._mcp_headers = {"Accept": BOTH}  # type: ignore[attr-defined]
    if session:
        c._mcp_headers["mcp-session-id"] = session  # type: ignore[attr-defined]
    c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=c._mcp_headers,  # type: ignore[attr-defined]
    )
    return c


def _call(client: TestClient, tool: str, **extra_params):
    params = {"name": tool, "arguments": {"x": 7}, **extra_params}
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": params},
        headers=client._mcp_headers,  # type: ignore[attr-defined]
    )


def _content_type(response) -> str:
    return response.headers.get("content-type", "").split(";")[0]


# ── the saving: nothing was emitted, so JSON is all there is ──────────


def test_a_streamless_tool_is_answered_as_json(client):
    """`plain` cannot reach the context, so its reply is the only message."""
    assert _content_type(_call(client, "plain")) == "application/json"


def test_the_json_reply_carries_the_same_result(client):
    """The shortcut changes the framing, never the answer."""
    result = json.loads(_call(client, "plain").text)["result"]
    assert json.loads(result["content"][0]["text"]) == {"x": 7}


@pytest.mark.parametrize("tool", ["takes_ctx", "dep_ctx"])
def test_a_context_reaching_tool_that_sends_nothing_is_answered_as_json(client, tool):
    """Being able to reach the context is not the same as using it."""
    assert _content_type(_call(client, tool)) == "application/json"


def test_a_method_that_is_not_a_tool_call_is_answered_as_json(client):
    """`tools/list` never notifies, so the narrowing is not scoped to calls."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
        headers=client._mcp_headers,  # type: ignore[attr-defined]
    )
    assert _content_type(response) == "application/json"


def test_an_unknown_tool_is_answered_as_json(client):
    """Its error reply is produced immediately, with nothing ahead of it."""
    assert _content_type(_call(client, "no_such_tool")) == "application/json"


def test_a_progress_token_alone_does_not_force_the_stream(client):
    """`plain` cannot reach the context, so no progress is ever possible."""
    response = _call(client, "plain", _meta={"progressToken": "p1"})
    assert _content_type(response) == "application/json"


# ── and anything that actually speaks first keeps its channel ─────────


def test_a_tool_that_notifies_keeps_the_stream(client):
    """Something arrived before the reply, so the call needed the stream."""
    assert _content_type(_call(client, "notifies")) == "text/event-stream"


def test_the_notification_arrives_before_the_result(client):
    """The stream carries what was already produced, in the order it happened."""
    response = _call(client, "notifies")
    body = response.text
    assert body.index('"method":"notifications/message"') < body.index('"result"')


def test_a_task_augmented_call_keeps_the_stream(client):
    """Its `notifications/tasks/status` follows the reply and needs the channel.

    The tool here is the streamless one, so only the `task` field can be what
    holds the stream open - which is the point: a client that asked for
    detached work must not be answered in a way that cannot tell it the work
    finished.
    """
    assert _content_type(_call(client, "plain", task={})) == "text/event-stream"


def test_a_client_that_refuses_json_still_gets_the_stream(client):
    """It named no other type it would take, so JSON is not an answer for it."""
    headers = dict(client._mcp_headers)  # type: ignore[attr-defined]
    headers["Accept"] = "text/event-stream"
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "plain", "arguments": {"x": 7}},
        },
        headers=headers,
    )
    assert _content_type(response) == "text/event-stream"


def test_waiting_for_a_reply_that_never_comes_falls_back_to_the_stream(client, monkeypatch):
    """A handler that outlasts the wait is not left with no answer at all."""
    monkeypatch.setattr(mcp_http, "_LAZY_REPLY_TIMEOUT_SECONDS", 0.05)
    response = _call(client, "slow")
    assert _content_type(response) == "text/event-stream"


async def test_cancelling_the_wait_cleans_up_and_does_not_wedge_the_app():
    """A dropped connection while waiting on the first message must not leave
    the dispatch's queue/client_gone state behind for the next request to
    trip over."""
    app = _app()

    async with AsyncTestClient(app) as client:
        init = await client.post("/mcp", json=INITIALIZE, headers={"Accept": BOTH})
        headers = {"Accept": BOTH}
        session = init.headers.get("mcp-session-id")
        if session:
            headers["mcp-session-id"] = session
        await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )

        call = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {"x": 7}},
        }
        task = asyncio.ensure_future(client.post("/mcp", json=call, headers=headers))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The connection still works afterward - nothing from the cancelled
        # call's dispatch state leaked into a later request.
        again = await client.post(
            "/mcp",
            json={**call, "id": 10, "params": {"name": "plain", "arguments": {"x": 1}}},
            headers=headers,
        )
        assert json.loads(again.text)["result"]["content"]


# ── a handler the walk cannot see through ─────────────────────────────


def test_a_tool_that_dispatches_other_tools_is_marked_may_stream():
    """`run_tools` calls other tools, and one of them may log through a context.

    Its own signature takes no `MCPContext`, so the flag is what a static walk
    of the plan cannot infer: where a handler sends the call next. The HTTP
    transport no longer reads this directly - it watches what the call actually
    produces - but the flag stays as a registration-time description of intent.
    """

    async def _run_tools(self, steps, stop_on_error=True):  # noqa: ANN001
        return []

    tool = _meta_tool(_run_tools, "run_tools", "Run several tools in order")
    assert tool.dispatches_tools is True
    assert tool.may_stream is True


def test_an_ordinary_streamless_tool_is_not_marked_as_dispatching():
    """The control: the flag is opt-in, not the default."""

    async def plain(x: int = 1):
        return {}

    tool = MCPTool(
        name="plain", description="d", handler=plain, plan=build_plan(plain), input_schema={}
    )
    assert tool.dispatches_tools is False
    assert tool.may_stream is False
