"""`subscriptions/listen` over the default Streamable HTTP deployment.

The modern revision removed sessions and made the open stream itself the
subscription, so a listen has to work on the shape `mount_mcp(transport="http")`
builds out of the box: one per-request session, one SSE stream, no session
store. Two things had to hold for that and neither did - the method refused any
connection that was not a *persistent* session, and the SSE runner ended the
stream as soon as the deferred request returned, which is immediately after the
acknowledgement.

These drive the ASGI app directly: the stream is long-lived by design, so the
test client's whole-response reader cannot observe it.
"""

from __future__ import annotations

import asyncio

import pytest

from tests._mcp import INVALID_PARAMS, MODERN_REVISION, PostStream
from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.subscriptions import META_SUBSCRIPTION_ID
from veloce.contrib.mcp.transports.http import register_http_transport

MODERN = MODERN_REVISION
_META = {"io.modelcontextprotocol/protocolVersion": MODERN}


def _app() -> tuple[Veloce, MCPServer]:
    """The default HTTP deployment: no session store, no resumability.

    Mounted through `register_http_transport` rather than `mount_mcp` only so the
    test keeps the server handle it needs to signal a change from the outside;
    the transport is configured exactly as `mount_mcp(transport="http")` leaves it.
    """
    app = Veloce(title="ListenHTTP", openapi_url=None)
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

    server = MCPServer(app)
    register_http_transport(app, server)
    return app, server


def _listen(notifications: dict, request_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "subscriptions/listen",
        "params": {"notifications": notifications, "_meta": _META},
    }


# ── The stream opens at all ──────────────────────────────────────────


async def test_a_listen_is_acknowledged_on_the_default_deployment():
    """The defect: this was refused as needing a stateful connection."""
    app, _ = _app()
    async with PostStream(app, _listen({"toolsListChanged": True})) as stream:
        ack = await stream.message()
        assert ack["method"] == "notifications/subscriptions/acknowledged"
        assert ack["params"]["_meta"][META_SUBSCRIPTION_ID] == 1
        assert stream.status == 200


async def test_the_acknowledgement_reports_only_what_is_honoured():
    """An unknown topic is dropped rather than echoed back as agreed."""
    app, _ = _app()
    async with PostStream(app, _listen({"toolsListChanged": True, "nonsense": True})) as stream:
        ack = await stream.message()
        assert ack["params"]["notifications"] == {"toolsListChanged": True}


# ── The stream stays open ────────────────────────────────────────────


async def test_the_stream_survives_its_acknowledgement_and_delivers():
    """The second half: the runner used to end the stream on the deferral."""
    app, server = _app()
    async with PostStream(app, _listen({"toolsListChanged": True})) as stream:
        await stream.message()  # the acknowledgement
        await server.notify_tools_list_changed()
        pushed = await stream.message()
        assert pushed["method"] == "notifications/tools/list_changed"
        assert pushed["params"]["_meta"][META_SUBSCRIPTION_ID] == 1


async def test_the_stream_keeps_delivering_more_than_once():
    """A subscription is long-lived, not a one-shot."""
    app, server = _app()
    async with PostStream(app, _listen({"toolsListChanged": True})) as stream:
        await stream.message()
        for _ in range(3):
            await server.notify_tools_list_changed()
            assert (await stream.message())["method"] == "notifications/tools/list_changed"


async def test_a_resource_update_reaches_a_uri_subscriber():
    """The other half of the filter: a named resource, not a topic."""
    app, server = _app()
    body = _listen({"resourceSubscriptions": ["res://one"]}, request_id=4)
    async with PostStream(app, body) as stream:
        await stream.message()
        await server.notify_resource_updated("res://one")
        pushed = await stream.message()
        assert pushed["method"] == "notifications/resources/updated"
        assert pushed["params"]["_meta"][META_SUBSCRIPTION_ID] == 4


async def test_an_unrequested_topic_is_not_delivered():
    """Holding the stream open must not widen what it agreed to receive."""
    app, server = _app()
    async with PostStream(app, _listen({"toolsListChanged": True})) as stream:
        await stream.message()
        await server.notify_prompts_list_changed()
        await server.notify_tools_list_changed()
        # The prompts notification was never agreed to, so the next payload on
        # the stream is the tools one that followed it.
        assert (await stream.message())["method"] == "notifications/tools/list_changed"


# ── The stream closes ────────────────────────────────────────────────


async def test_the_connection_is_released_when_the_client_hangs_up():
    """A vanished client must not leave its registration behind."""
    app, server = _app()
    async with PostStream(app, _listen({"toolsListChanged": True})) as stream:
        await stream.message()
        assert server._connections is not None
        assert server._connections._sinks
        await stream.hang_up()
    assert server._connections._sinks == {}


async def test_a_hung_up_stream_receives_nothing_further():
    """The fan-out must not walk a connection whose transport is gone."""
    app, server = _app()
    async with PostStream(app, _listen({"toolsListChanged": True})) as stream:
        await stream.message()
        await stream.hang_up()
        await server.notify_tools_list_changed()
        with pytest.raises(asyncio.TimeoutError):
            await stream.message(timeout=0.25)


# ── A non-streaming POST is still refused ────────────────────────────


async def test_a_plain_json_post_cannot_open_a_stream():
    """Without `Accept: text/event-stream` there is no stream to deliver on."""
    app, _ = _app()
    body = _listen({"toolsListChanged": True})
    async with PostStream(app, body, accept=b"application/json") as stream:
        payload = await stream.whole_body()
        assert payload["error"]["code"] == INVALID_PARAMS
        assert "open stream" in payload["error"]["message"]


async def test_an_ordinary_call_still_ends_its_stream():
    """The hold applies to a listen only - a normal call must still complete."""
    app, server = _app()
    body = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "a_tool", "_meta": _META},
    }
    async with PostStream(app, body, name=b"a_tool") as stream:
        reply = await stream.message()
        assert reply["id"] == 3
        # The stream ends on its own rather than hanging: the task completes
        # without the client having to disconnect.
        await asyncio.wait_for(asyncio.shield(stream.task), 5.0)
