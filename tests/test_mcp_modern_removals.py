"""What the modern revision removed, and what replaced it.

`ping` and `logging/setLevel` are gone, and the log level moved from a
connection-wide setting to a per-request one. The rule that follows is the sharp
part: a request that names no level receives no `notifications/message` at all.
A handshake-era client keeps everything its revision defined.
"""

from __future__ import annotations

import pytest

from veloce import MCPContext, Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession

MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
LOG_LEVEL_KEY = "io.modelcontextprotocol/logLevel"


def _app() -> Veloce:
    app = Veloce(title="RemovalProbe", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Emit one log line at each level")
    async def chatter(ctx: MCPContext) -> dict:
        await ctx.debug("d")
        await ctx.info("i")
        await ctx.warning("w")
        await ctx.error("e")
        return {"ok": True}

    return app


class Harness:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.server = MCPServer(_app())
        # What collects is the notifier, not the app: the factory used to take
        # `self.sent` and ignore it, which read as a wiring that is not there.
        self.server.set_notifier(self.send)
        self.session = MCPSession()

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def call(self, meta: dict | None) -> dict:
        params: dict = {"name": "chatter", "arguments": {}}
        if meta is not None:
            params["_meta"] = meta
        return await self.server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
            self.session,
        )

    def levels(self) -> list[str]:
        return [
            m["params"]["level"] for m in self.sent if m.get("method") == "notifications/message"
        ]


# ── Removed methods ──────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["ping", "logging/setLevel"])
async def test_a_removed_method_is_not_found_for_a_modern_client(method: str):
    harness = Harness()
    response = await harness.server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {"level": "debug", "_meta": MODERN},
        },
        harness.session,
    )
    assert response["error"]["code"] == -32601


@pytest.mark.parametrize("method", ["ping", "logging/setLevel"])
async def test_a_removed_method_still_serves_a_handshake_client(method: str):
    harness = Harness()
    response = await harness.server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"level": "debug"}},
        harness.session,
    )
    assert "error" not in response


# ── Per-request log level ────────────────────────────────────────────


async def test_a_modern_request_naming_no_level_gets_no_log_notifications():
    """The spec's MUST NOT: no level named, nothing emitted."""
    harness = Harness()
    await harness.call(MODERN)
    assert harness.levels() == []


async def test_a_modern_request_naming_a_level_filters_to_it():
    harness = Harness()
    await harness.call({**MODERN, LOG_LEVEL_KEY: "warning"})
    assert harness.levels() == ["warning", "error"]


async def test_a_modern_request_naming_debug_receives_everything():
    harness = Harness()
    await harness.call({**MODERN, LOG_LEVEL_KEY: "debug"})
    assert harness.levels() == ["debug", "info", "warning", "error"]


async def test_an_invalid_level_is_treated_as_none_named():
    harness = Harness()
    await harness.call({**MODERN, LOG_LEVEL_KEY: "chatty"})
    assert harness.levels() == []


async def test_the_level_does_not_leak_into_the_next_request():
    """Per-request means per-request: the context var is reset each message."""
    harness = Harness()
    await harness.call({**MODERN, LOG_LEVEL_KEY: "debug"})
    assert len(harness.levels()) == 4
    harness.sent.clear()
    await harness.call(MODERN)
    assert harness.levels() == []


async def test_a_handshake_request_still_logs_without_naming_a_level():
    """The old revision set the level once per connection and defaulted to all."""
    harness = Harness()
    await harness.call(None)
    assert harness.levels() == ["debug", "info", "warning", "error"]


async def test_logging_set_level_still_governs_a_handshake_client():
    harness = Harness()
    await harness.server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "logging/setLevel", "params": {"level": "error"}},
        harness.session,
    )
    await harness.call(None)
    assert harness.levels() == ["error"]
