"""Wall-time benchmark for a configured MCP tool-visibility policy.

A synchronous policy is kept off the event loop, so most of its cost is a thread
handoff that instruction counting does not observe. The measurement that matters is
that the handoff happens **once per listing** rather than once per tool: this pins
that promise, since a regression to per-tool offloading would not change the
instruction count materially but would multiply the wall time by the tool count.
"""

from __future__ import annotations

from benchmarks.conftest import run_async
from veloce import Principal, Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.principal import set_principal

TOOL_COUNT = 50


def _build_app() -> Veloce:
    app = Veloce(openapi_url=None)

    for index in range(TOOL_COUNT):

        async def handler(value: int = 0, _index: int = index) -> dict:
            return {"index": _index, "value": value}

        handler.__name__ = f"tool_{index}"
        app.mcp_tool(description=f"Tool number {index}")(handler)

    return app


APP = _build_app()
FILTERED_SERVER = MCPServer(APP, tool_filter=lambda tool, principal: True)
run_async(FILTERED_SERVER._handle_tools_list({}))


def test_mcp_tools_list_with_sync_filter(benchmark):
    """Listing with a synchronous visibility policy — one offload for the pass."""
    set_principal(Principal(subject="bench", scopes=frozenset()))
    result = benchmark(lambda: run_async(FILTERED_SERVER._handle_tools_list({})))
    assert len(result["tools"]) == TOOL_COUNT


async def _always(tool: object, principal: object) -> bool:
    return True


ASYNC_FILTERED_SERVER = MCPServer(APP, tool_filter=_always)
run_async(ASYNC_FILTERED_SERVER._handle_tools_list({}))


def test_mcp_tools_list_with_async_filter(benchmark):
    """The same listing with an async policy — awaited inline, no thread hop."""
    set_principal(Principal(subject="bench", scopes=frozenset()))
    result = benchmark(lambda: run_async(ASYNC_FILTERED_SERVER._handle_tools_list({})))
    assert len(result["tools"]) == TOOL_COUNT
