"""A tool call is answered as a stream only when it can produce one.

The spec lets a POST carrying a request be answered either as JSON or as an SSE
stream, and tells clients to accept both - so choosing on the `Accept` header
alone means a conformant client pays for stream framing on every call,
including one that emits exactly its own reply. Measured, that framing is a
third of the cost of a plain tool call.

What decides it instead is whether the call can produce a second message. Only
a handler that can reach the `MCPContext` emits progress, logging, a sampling
or an elicitation message, and the registry settles that per tool at
registration. Everything else keeps the stream, because the cost of being wrong
is a client that stops hearing about work it asked for: a task-augmented call
whose `notifications/tasks/status` follows the reply, a client that asked for
progress, or a server whose resumability replay needs the event ids the stream
carries.
"""

from __future__ import annotations

import json

import pytest

from tests._mcp import initialize
from veloce import Depends, Veloce
from veloce._handler_plan import build_plan
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.registry import MCPTool
from veloce.contrib.mcp.toolsearch import _meta_tool
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

    @app.get("/ctx", expose_as_mcp_tool=True, mcp_description="Takes the context")
    async def takes_ctx(ctx: MCPContext, x: int = 1) -> dict:
        return {"x": x}

    @app.get("/depctx", expose_as_mcp_tool=True, mcp_description="A dependency takes it")
    async def dep_ctx(d: int = Depends(_ctx_dependency), x: int = 1) -> dict:
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


# ── the saving ────────────────────────────────────────────────────────


def test_a_streamless_tool_is_answered_as_json(client):
    """`plain` cannot reach the context, so its reply is the only message."""
    assert _content_type(_call(client, "plain")) == "application/json"


def test_the_json_reply_carries_the_same_result(client):
    """The shortcut changes the framing, never the answer."""
    result = json.loads(_call(client, "plain").text)["result"]
    assert json.loads(result["content"][0]["text"]) == {"x": 7}


# ── and everything that could still speak keeps its channel ───────────


@pytest.mark.parametrize("tool", ["takes_ctx", "dep_ctx"])
def test_a_tool_that_can_reach_the_context_keeps_the_stream(client, tool):
    """Directly or through a dependency - either can report progress."""
    assert _content_type(_call(client, tool)) == "text/event-stream"


def test_a_task_augmented_call_keeps_the_stream(client):
    """Its `notifications/tasks/status` follows the reply and needs the channel.

    The tool here is the streamless one, so only the `task` field can be what
    holds the stream open - which is the point: a client that asked for
    detached work must not be answered in a way that cannot tell it the work
    finished.
    """
    assert _content_type(_call(client, "plain", task={})) == "text/event-stream"


def test_a_progress_token_keeps_the_stream(client):
    """The client asked to be kept informed; give it somewhere to be informed."""
    response = _call(client, "plain", _meta={"progressToken": "p1"})
    assert _content_type(response) == "text/event-stream"


def test_a_method_that_is_not_a_tool_call_keeps_the_stream(client):
    """The narrowing is scoped to `tools/call` and nothing else."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
        headers=client._mcp_headers,  # type: ignore[attr-defined]
    )
    assert _content_type(response) == "text/event-stream"


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


def test_an_unknown_tool_keeps_the_stream(client):
    """Nothing to classify, so the reply shape stays what it was."""
    assert _content_type(_call(client, "no_such_tool")) == "text/event-stream"


# ── a handler the walk cannot see through ─────────────────────────────


def test_a_tool_that_dispatches_other_tools_keeps_the_stream():
    """`run_tools` calls other tools, and one of them may log through a context.

    Its own signature takes no `MCPContext`, so the plan walk alone would call
    it streamless - and every message from the tools it ran would be dropped on
    a 200 with a well-formed result. The flag is what the walk cannot infer:
    where a handler sends the call next.
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
