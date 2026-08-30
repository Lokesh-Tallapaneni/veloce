"""The shared MCP harnesses behave like the copies they replace.

`tests/_mcp.py` is test infrastructure for a thirty-five-module cluster, so it
gets its own tests: a `Pipe` that silently dropped a reply, or an `SSEStream`
whose frame parser mis-split, would make failures appear in whichever module
happened to notice.
"""

from __future__ import annotations

import pytest

from tests._mcp import (
    AUTHORIZATION_SERVER_URL,
    METHOD_NOT_FOUND,
    RESOURCE_SERVER_URL,
    Pipe,
    SSEStream,
    accepts_any,
    accepts_good,
    auth,
    call,
    call_error,
    call_raw,
)
from veloce import Veloce
from veloce.contrib.mcp import MCPServer

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}


def _app() -> Veloce:
    app = Veloce(title="H", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    return app


def _server(app: Veloce):

    return MCPServer(app)


# ── Pipe ─────────────────────────────────────────────────────────────


async def test_pipe_returns_a_reply_per_request():
    pipe = Pipe(_server(_app()))
    pipe.feed(_INIT)
    assert len(await pipe.run()) == 1


async def test_pipe_decodes_the_reply():
    pipe = Pipe(_server(_app()))
    pipe.feed(_INIT)
    (reply,) = await pipe.run()
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == 1


async def test_pipe_preserves_order():
    pipe = Pipe(_server(_app()))
    pipe.feed(_INIT)
    pipe.feed({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    replies = await pipe.run()
    assert [r["id"] for r in replies] == [1, 2]


async def test_pipe_feed_chains():
    """It returns self, so a sequence can be written as one expression."""
    pipe = Pipe(_server(_app()))
    replies = await pipe.feed(_INIT).feed({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).run()
    assert len(replies) == 2


async def test_pipe_with_no_input_returns_nothing():
    """The negative: an empty inbox must terminate, not block."""
    assert await Pipe(_server(_app())).run() == []


async def test_pipe_exposes_the_outbox():
    pipe = Pipe(_server(_app()))
    pipe.feed(_INIT)
    await pipe.run()
    assert pipe.outbox and pipe.outbox[0]["id"] == 1


# ── auth ─────────────────────────────────────────────────────────────


def test_auth_uses_the_cluster_urls():
    built = auth()
    assert built.resource_server_url == RESOURCE_SERVER_URL
    assert list(built.authorization_servers) == [AUTHORIZATION_SERVER_URL]


def test_auth_takes_a_custom_verifier():
    sentinel = object()
    assert auth(lambda token: sentinel).verify("x") is sentinel


def test_auth_urls_can_be_overridden():
    built = auth(resource_server_url="https://other.example/mcp")
    assert built.resource_server_url == "https://other.example/mcp"


def test_the_default_verifier_accepts_only_good():
    verify = auth().verify
    assert verify("good") is not None
    assert verify("bad") is None
    assert verify("") is None


def test_accepts_good_returns_a_principal():
    principal = accepts_good("good")
    assert principal is not None
    assert principal.subject == "s"


@pytest.mark.parametrize("token", ["anything", "x", "good"])
def test_accepts_any_accepts_everything(token):
    assert accepts_any(token) is not None


def test_accepts_any_names_an_empty_token():
    assert accepts_any("").subject == "anonymous"


# ── SSEStream ────────────────────────────────────────────────────────


def _sse_app() -> Veloce:
    app = _app()
    app.mount_mcp(transport="sse", path="/sse")
    return app


async def test_the_stream_opens_and_announces_an_endpoint():
    async with SSEStream(_sse_app()) as stream:
        assert (await stream.event())["event"] == "endpoint"


async def test_the_stream_reports_its_status():
    async with SSEStream(_sse_app()) as stream:
        assert await stream.wait_status() == 200


async def test_wait_status_reports_a_refusal():
    """The case the fixed sleeps existed for: a refused stream sends only a
    start message, so `event()` would wait forever."""
    app = _app()
    app.mount_mcp(transport="sse", path="/sse", allowed_origins=["https://app.example.com"])
    async with SSEStream(
        app,
        headers=[(b"origin", b"https://evil.example")],
    ) as stream:
        assert await stream.wait_status() == 403


async def test_a_custom_path_is_used():
    app = _app()
    app.mount_mcp(transport="sse", path="/agent/sse")
    async with SSEStream(app, path="/agent/sse") as stream:
        assert (await stream.event())["event"] == "endpoint"


async def test_the_accept_header_is_always_present():
    """It selects the SSE branch; a caller passing headers must not lose it."""
    async with SSEStream(_sse_app(), headers=[(b"x-probe", b"1")]) as stream:
        assert await stream.wait_status() == 200


async def test_the_frame_parser_splits_on_a_blank_line():
    """Two frames must not merge into one."""
    async with SSEStream(_sse_app()) as stream:
        first = await stream.event()
        assert set(first) <= {"event", "data", "id", "retry"}
        assert first["event"] == "endpoint"


async def test_exiting_cancels_the_stream():
    stream = SSEStream(_sse_app())
    async with stream:
        await stream.event()
    assert stream.task is not None
    assert stream.task.cancelled() or stream.task.done()


async def test_settled_yields_without_sleeping():
    """It must return promptly - it replaced a fixed 0.05s sleep."""
    async with SSEStream(_sse_app()) as stream:
        await stream.event()
    await stream.settled()


# ── call / call_raw / call_error ─────────────────────────────────────
#
# Eleven modules called the private handler behind a method to skip writing the
# JSON-RPC envelope. Going through `handle_message` costs one helper and buys
# the dispatch map, the in-flight tracking and the error shaping - so a method
# registered under the wrong name fails here instead of passing and failing for
# a real client.


async def test_call_returns_the_result():
    server = _server(_app())
    await call(server, "initialize", _INIT["params"])
    assert "tools" in await call(server, "tools/list")


async def test_call_passes_params():
    server = _server(_app())
    await call(server, "initialize", _INIT["params"])
    result = await call(server, "tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}})
    assert "5" in result["content"][0]["text"]


async def test_call_raises_with_the_error_attached():
    """An opaque `KeyError: 'result'` says nothing about what went wrong."""
    server = _server(_app())
    with pytest.raises(AssertionError, match="tools/call failed"):
        await call(server, "tools/call", {"name": "nope", "arguments": {}})


async def test_call_error_returns_the_error_object():
    server = _server(_app())
    error = await call_error(server, "tools/call", {"name": "nope", "arguments": {}})
    assert "code" in error


async def test_call_error_refuses_a_success():
    """The negative: it must not silently pass when the call worked."""
    server = _server(_app())
    await call(server, "initialize", _INIT["params"])
    with pytest.raises(AssertionError, match="unexpectedly succeeded"):
        await call_error(server, "tools/list")


async def test_call_raw_returns_the_envelope():
    server = _server(_app())
    envelope = await call_raw(server, "initialize", _INIT["params"])
    assert envelope is not None
    assert envelope["jsonrpc"] == "2.0"
    assert envelope["id"] == 1


async def test_a_notification_has_no_response():
    assert await call_raw(_server(_app()), "notifications/initialized", id=None) is None


async def test_an_unknown_method_is_method_not_found():
    """It goes through the dispatch map, which is the point."""
    error = await call_error(_server(_app()), "no/such/method")
    assert error["code"] == METHOD_NOT_FOUND


# ── the capability registry ──────────────────────────────────────────


def test_a_server_reports_its_capabilities():
    assert _server(_app()).capabilities


def test_the_capabilities_are_a_tuple():
    """Read-only: the set is decided at construction and the method map, the
    handshake-only set and the era-aware set are all derived from it there."""
    assert isinstance(_server(_app()).capabilities, tuple)


def test_the_property_is_the_registry():
    server = _server(_app())
    assert server.capabilities == server._capabilities


def test_a_capability_names_the_methods_it_owns():
    """What the property is for: asking what a server actually supports."""
    server = _server(_app())
    owned = {method for capability in server.capabilities for method in capability.handlers()}
    assert "tools/list" in owned
