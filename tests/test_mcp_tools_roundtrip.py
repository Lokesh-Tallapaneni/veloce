"""`tools/list` and `tools/call` over the stdio transport.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import asyncio

import orjson

from tests._mcp import METHOD_NOT_FOUND, PARSE_ERROR, Pipe
from tests._mcp_shared import (
    Item,
    _server,
)
from veloce import (
    Veloce,
)
from veloce.contrib.mcp.transports.base import BidirectionalTransport, Transport
from veloce.contrib.mcp.transports.stdio import StdioTransport

# -- tools/list + tools/call round-trip -------------------------------


def test_tools_list_and_call_round_trip():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    pipe.feed({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 5}},
        }
    )
    out = asyncio.run(pipe.run())

    init, listed, called = out
    assert init["result"]["serverInfo"]["name"]
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "add" in names
    assert called["result"]["content"][0]["text"] == "7"


def test_call_coerces_string_arguments():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Double an integer")
    async def double(n: int) -> int:
        return n * 2

    pipe = Pipe(_server(app))
    # JSON argument arrives as a string; the bridge coerces it to int.
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "double", "arguments": {"n": "21"}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "42"


def test_call_pydantic_body_model():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Summarise an item")
    async def summarise(item: Item) -> str:
        return f"{item.qty}x {item.name}"

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "summarise", "arguments": {"item": {"name": "widget", "qty": 3}}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "3x widget"


def test_missing_required_argument_is_an_in_band_tool_error():
    """A missing required argument is reported in band, not on the error channel.
    An argument-binding failure is a **tool execution** error reported in band
    (`result.isError`), not a JSON-RPC transport error. The spec reserves the
    error channel for an unknown tool, a malformed request or a server fault,
    and clients feed only execution errors back to the model - reporting a bad
    argument there would deny the model the one signal it can act on.

    Named for that. It used to be `..._is_invalid_params`, with a docstring and
    a leading comment both asserting the opposite of the assertion below; a
    later round added a rebuttal comment above the assertion rather than
    correcting the name and the prose, so the test read as self-contradictory.
    """
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 1}},
        }
    )
    out = asyncio.run(pipe.run())
    # Input validation is a *tool execution* error, not a protocol error: the
    # spec reserves the JSON-RPC channel for an unknown tool, a malformed
    # request, or a server fault, and clients feed only execution errors back
    # to the model. Reporting a bad argument on the error channel would deny
    # the model the one signal it can act on.
    assert out[0]["result"]["isError"] is True
    # Names the argument the model left out, so it can supply it and retry.
    assert "b" in out[0]["result"]["content"][0]["text"]


def test_handler_internal_type_error_is_in_band():
    """A TypeError raised inside the handler body is an in-band tool error."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Trip on a bad operand inside the body")
    async def buggy(n: int) -> int:
        # A genuine handler bug raises TypeError; it must not be misread as an
        # invalid-params transport error.
        return n + "x"  # type: ignore[operator]

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "buggy", "arguments": {"n": 1}},
        }
    )
    out = asyncio.run(pipe.run())
    # The handler error is surfaced in-band (isError=true), never on the
    # JSON-RPC error channel.
    assert "error" not in out[0]
    assert out[0]["result"]["isError"] is True


def test_unknown_method_returns_method_not_found():
    app = Veloce(openapi_url=None)
    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 9, "method": "does/not/exist", "params": {}})
    out = asyncio.run(pipe.run())
    assert out[0]["error"]["code"] == METHOD_NOT_FOUND


def test_notification_yields_no_response():
    app = Veloce(openapi_url=None)
    pipe = Pipe(_server(app))
    # No `id` -> notification, no response written.
    pipe.feed({"jsonrpc": "2.0", "method": "notifications/initialized"})
    out = asyncio.run(pipe.run())
    assert out == []


def test_parse_error_on_bad_json():
    app = Veloce(openapi_url=None)
    server = _server(app)
    transport = StdioTransport(server, None, None)  # type: ignore[arg-type]
    out = transport._decode(b"{not json")[1]
    assert out["error"]["code"] == PARSE_ERROR


def test_stdio_transport_satisfies_transport_contract():

    app = Veloce(openapi_url=None)
    transport = StdioTransport(_server(app), None, None)  # type: ignore[arg-type]
    assert isinstance(transport, Transport)


def test_stdio_transport_satisfies_bidirectional_contract():

    app = Veloce(openapi_url=None)
    transport = StdioTransport(_server(app), None, None)  # type: ignore[arg-type]
    assert isinstance(transport, BidirectionalTransport)


def test_stdio_transport_send_writes_one_message():
    app = Veloce(openapi_url=None)
    written: list[bytes] = []

    async def write_line(data: bytes) -> None:
        written.append(data)

    transport = StdioTransport(_server(app), None, write_line)  # type: ignore[arg-type]
    asyncio.run(transport.send({"jsonrpc": "2.0", "method": "notifications/progress"}))
    assert orjson.loads(written[0])["method"] == "notifications/progress"
