"""`subscriptions/listen` — the long-lived notification stream.

The spec's hard rules, each pinned here: the acknowledgement comes first and
reports only what the server will honour; a notification type the client did not
request is never sent; every message on the stream carries the subscription id;
and the stream's long-lived request is answered only when it closes.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.subscriptions import META_SUBSCRIPTION_ID

MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}


def _app() -> Veloce:
    app = Veloce(title="ListenProbe", openapi_url=None)
    app.config["MCP_RESOURCE_SUBSCRIPTIONS"] = True

    @app.mcp_tool(description="A tool")
    async def a_tool() -> dict:
        return {"ok": True}

    @app.get(
        "/c",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://one",
        mcp_description="A resource",
    )
    async def one() -> dict:
        return {"v": 1}

    return app


class Connection:
    """A registered connection whose outbound messages are captured."""

    def __init__(self, server: MCPServer) -> None:
        self.sent: list[dict] = []
        self.session = MCPSession()
        self.server = server
        server.set_notifier(self.send)
        self.token = server.register_connection(self.session, self.send)

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def listen(self, notifications: dict, request_id: int = 1) -> dict | None:
        return await self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "subscriptions/listen",
                "params": {"notifications": notifications, "_meta": MODERN},
            },
            self.session,
        )

    def methods(self) -> list[str]:
        return [m.get("method") for m in self.sent if "method" in m]

    def ids(self) -> list:
        return [
            (m.get("params") or {}).get("_meta", {}).get(META_SUBSCRIPTION_ID) for m in self.sent
        ]


def _server() -> MCPServer:
    return MCPServer(_app())


# ── Opening a stream ─────────────────────────────────────────────────


async def test_listen_produces_no_immediate_response():
    """The long-lived request is answered when the stream closes, not now."""
    conn = Connection(_server())
    assert await conn.listen({"toolsListChanged": True}) is None


async def test_the_acknowledgement_is_the_first_message():
    conn = Connection(_server())
    await conn.listen({"toolsListChanged": True})
    assert conn.methods()[0] == "notifications/subscriptions/acknowledged"


async def test_the_acknowledgement_carries_the_subscription_id():
    conn = Connection(_server())
    await conn.listen({"toolsListChanged": True}, request_id=7)
    assert conn.sent[0]["params"]["_meta"][META_SUBSCRIPTION_ID] == 7


async def test_the_acknowledgement_reports_the_agreed_filter():
    conn = Connection(_server())
    await conn.listen({"toolsListChanged": True, "resourceSubscriptions": ["res://one"]})
    agreed = conn.sent[0]["params"]["notifications"]
    assert agreed == {"toolsListChanged": True, "resourceSubscriptions": ["res://one"]}


async def test_an_unknown_or_false_filter_entry_is_not_agreed():
    conn = Connection(_server())
    await conn.listen({"toolsListChanged": False, "somethingElse": True})
    assert conn.sent[0]["params"]["notifications"] == {}


async def test_a_missing_filter_agrees_to_nothing():
    conn = Connection(_server())
    await conn.listen({})
    assert conn.sent[0]["params"]["notifications"] == {}


# ── Delivery is limited to what was requested ────────────────────────


async def test_a_requested_topic_is_delivered_with_its_id():
    conn = Connection(_server())
    await conn.listen({"toolsListChanged": True}, request_id=3)
    conn.sent.clear()
    await conn.server.notify_tools_list_changed()
    assert conn.methods() == ["notifications/tools/list_changed"]
    assert conn.ids() == [3]


async def test_an_unrequested_topic_is_never_delivered():
    """The spec forbids sending a type the client did not ask for."""
    conn = Connection(_server())
    await conn.listen({"promptsListChanged": True})
    conn.sent.clear()
    await conn.server.notify_tools_list_changed()
    assert conn.methods() == []


async def test_a_resource_update_reaches_only_a_named_uri():
    conn = Connection(_server())
    await conn.listen({"resourceSubscriptions": ["res://one"]}, request_id=5)
    conn.sent.clear()
    await conn.server.notify_resource_updated("res://other")
    assert conn.methods() == []
    await conn.server.notify_resource_updated("res://one")
    assert conn.methods() == ["notifications/resources/updated"]
    assert conn.ids() == [5]


async def test_two_streams_each_receive_only_their_own_topics():
    conn = Connection(_server())
    await conn.listen({"toolsListChanged": True}, request_id=1)
    await conn.listen({"promptsListChanged": True}, request_id=2)
    conn.sent.clear()
    await conn.server.notify_tools_list_changed()
    await conn.server.notify_prompts_list_changed()
    assert list(zip(conn.methods(), conn.ids(), strict=True)) == [
        ("notifications/tools/list_changed", 1),
        ("notifications/prompts/list_changed", 2),
    ]


# ── Closing ──────────────────────────────────────────────────────────


async def test_cancelling_the_request_closes_the_stream():
    conn = Connection(_server())
    await conn.listen({"toolsListChanged": True}, request_id=4)
    conn.sent.clear()
    await conn.server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 4}},
        conn.session,
    )
    closing = conn.sent[-1]
    assert closing["id"] == 4
    assert closing["result"]["resultType"] == "complete"
    assert closing["result"]["_meta"][META_SUBSCRIPTION_ID] == 4
    assert conn.session.listen_streams == {}


async def test_a_closed_stream_receives_nothing_further():
    conn = Connection(_server())
    await conn.listen({"toolsListChanged": True}, request_id=4)
    await conn.server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 4}},
        conn.session,
    )
    conn.sent.clear()
    await conn.server.notify_tools_list_changed()
    assert conn.methods() == []


async def test_cancelling_an_unknown_id_is_ignored():
    conn = Connection(_server())
    await conn.listen({"toolsListChanged": True}, request_id=4)
    conn.sent.clear()
    await conn.server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}},
        conn.session,
    )
    assert conn.sent == []
    assert 4 in conn.session.listen_streams


# ── Statefulness ─────────────────────────────────────────────────────


async def test_a_stateless_request_cannot_open_a_stream():
    """A stream is per-connection state; a stateless POST holds none."""
    server = _server()
    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "subscriptions/listen",
            "params": {"notifications": {"toolsListChanged": True}, "_meta": MODERN},
        },
        MCPSession(persistent=False),
    )
    assert "error" in response


async def test_the_handshake_subscribe_path_still_works():
    """`resources/subscribe` must keep working for a handshake-era client."""
    conn = Connection(_server())
    await conn.server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/subscribe",
            "params": {"uri": "res://one"},
        },
        conn.session,
    )
    conn.sent.clear()
    await conn.server.notify_resource_updated("res://one")
    assert conn.methods() == ["notifications/resources/updated"]
    # No listen stream, so no subscription id is stamped.
    assert conn.ids() == [None]


@pytest.mark.parametrize("notifications", ["not-a-dict", 42, None])
async def test_a_malformed_filter_agrees_to_nothing(notifications):
    conn = Connection(_server())
    await conn.server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "subscriptions/listen",
            "params": {"notifications": notifications, "_meta": MODERN},
        },
        conn.session,
    )
    assert conn.sent[0]["params"]["notifications"] == {}


async def test_a_dropped_connection_forgets_its_streams():
    """A closed transport cannot receive a graceful close, so nothing is sent."""
    server = _server()
    conn = Connection(server)
    await conn.listen({"toolsListChanged": True}, request_id=8)
    conn.sent.clear()
    server.unregister_connection(conn.token)
    assert conn.session.listen_streams == {}
    await server.notify_tools_list_changed()
    assert conn.sent == []
