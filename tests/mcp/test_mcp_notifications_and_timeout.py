"""Progress and logging notifications, the per-call timeout, and error-text gating.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import asyncio

from tests._mcp import INTERNAL_ERROR, INVALID_PARAMS, Pipe
from tests._mcp_shared import (
    _call,
    _drive_call,
    _initialize,
    _read_resource,
    _server,
)
from veloce import (
    MCPContext,
    Veloce,
)

# -- Progress / logging notifications ---------------------------------


def test_progress_notification_emitted_with_token():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work with progress")
    async def work(ctx: MCPContext) -> str:
        await ctx.report_progress(1, 2)
        await ctx.report_progress(2, 2)
        return "done"

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "work", "arguments": {}, "_meta": {"progressToken": "p1"}},
        }
    )
    out = asyncio.run(pipe.run())

    progresses = [m for m in out if m.get("method") == "notifications/progress"]
    assert len(progresses) == 2
    assert progresses[0]["params"] == {"progressToken": "p1", "progress": 1, "total": 2}
    # The result is written after the in-call progress notifications.
    result = next(m for m in out if m.get("id") == 1)
    assert result["result"]["content"][0]["text"] == "done"
    assert out[-1] is result


def test_progress_is_noop_without_token():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Work with progress")
    async def work(ctx: MCPContext) -> str:
        await ctx.report_progress(1, 2)
        return "done"

    # No `_meta.progressToken`, so the client did not opt into progress.
    out = asyncio.run(_drive_call(app, "work"))
    assert [m for m in out if m.get("method") == "notifications/progress"] == []


def test_log_notification_emitted():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Log then return")
    async def work(ctx: MCPContext) -> str:
        await ctx.log("info", "working")
        return "ok"

    out = asyncio.run(_drive_call(app, "work"))
    messages = [m for m in out if m.get("method") == "notifications/message"]
    assert len(messages) == 1
    assert messages[0]["params"]["level"] == "info"
    assert messages[0]["params"]["data"] == "working"


def test_log_filtered_below_set_level():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Log then return")
    async def work(ctx: MCPContext) -> str:
        await ctx.log("info", "noisy")
        return "ok"

    pipe = Pipe(_server(app))
    # Raise the minimum to `error`, then call: the `info` log is below it.
    pipe.feed(
        {"jsonrpc": "2.0", "id": 1, "method": "logging/setLevel", "params": {"level": "error"}}
    )
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "work", "arguments": {}},
        }
    )
    out = asyncio.run(pipe.run())

    assert next(m for m in out if m.get("id") == 1)["result"] == {}
    assert [m for m in out if m.get("method") == "notifications/message"] == []


def test_logging_set_level_rejects_invalid_level():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    pipe = Pipe(_server(app))
    pipe.feed(
        {"jsonrpc": "2.0", "id": 1, "method": "logging/setLevel", "params": {"level": "verbose"}}
    )
    out = asyncio.run(pipe.run())
    assert out[0]["error"]["code"] == INVALID_PARAMS


def test_logging_capability_advertised():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    caps = _initialize(app, {})["result"]["capabilities"]
    assert caps["logging"] == {}


# -- Per-call timeout (MCP_CALL_TIMEOUT) ------------------------------


def test_tool_call_timeout_is_in_band_error():
    app = Veloce(openapi_url=None)
    app.config["MCP_CALL_TIMEOUT"] = 0.05

    @app.mcp_tool(description="Hangs forever")
    async def hang() -> str:
        await asyncio.sleep(10)
        return "never"

    result = _call(app, "hang", {})["result"]
    assert result["isError"] is True
    assert "timeout" in result["content"][0]["text"].lower()


def test_resource_read_timeout_is_error():
    app = Veloce(openapi_url=None)
    app.config["MCP_CALL_TIMEOUT"] = 0.05

    @app.get(
        "/slow",
        expose_as_mcp_resource=True,
        mcp_resource_uri="slow://data",
        mcp_description="Slow resource",
    )
    async def slow() -> dict:
        await asyncio.sleep(10)
        return {}

    out = _read_resource(app, "slow://data")
    assert out["error"]["code"] == INTERNAL_ERROR


def test_no_timeout_by_default_completes():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Brief await")
    async def brief() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    # With no MCP_CALL_TIMEOUT configured, the call runs unbounded and completes.
    result = _call(app, "brief", {})["result"]
    assert result["content"][0]["text"] == "ok"


# -- Error-text gating (debug) ----------------------------------------


def test_pure_tool_error_text_is_generic_without_debug():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Leaks a secret in its error")
    async def boom() -> str:
        raise RuntimeError("postgres://user:hunter2@db/secret")

    result = _call(app, "boom", {})["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    # The raw exception text (carrying a credential) is not surfaced.
    assert "hunter2" not in text
    assert text == "the tool raised an internal error"


def test_pure_tool_error_text_shown_with_debug():
    app = Veloce(openapi_url=None, debug=True)

    @app.mcp_tool(description="Surfaces its error in debug")
    async def boom() -> str:
        raise RuntimeError("a helpful development message")

    result = _call(app, "boom", {})["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "a helpful development message"
