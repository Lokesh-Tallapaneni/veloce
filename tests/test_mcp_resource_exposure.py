"""Exposing routes as MCP resources.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import asyncio
import base64

import orjson
import pytest

from tests._mcp import INVALID_PARAMS, METHOD_NOT_FOUND, RESOURCE_NOT_FOUND, Pipe
from tests._mcp_shared import (
    PublicUser,
    _initialize,
    _list_resource_templates,
    _list_resources,
    _read_resource,
    _server,
    _subscriptions_app,
)
from veloce import (
    Depends,
    HTTPException,
    Response,
    Veloce,
)
from veloce.contrib.mcp import MCPSession
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.subscriptions import ConnectionRegistry

# -- Resources --------------------------------------------------------


def test_static_resource_is_listed():
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        summary="App settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app/settings",
        mcp_description="The application settings",
    )
    async def settings() -> dict:
        return {"debug": False}

    listed = _list_resources(app)
    assert "config://app/settings" in listed
    entry = listed["config://app/settings"]
    assert entry["name"] == "settings"
    assert entry["title"] == "App settings"
    assert entry["description"] == "The application settings"
    # A static resource is not advertised as a template.
    assert "config://app/settings" not in _list_resource_templates(app)


def test_template_resource_is_listed_as_template():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{user_id}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        return {"id": user_id}

    templates = _list_resource_templates(app)
    assert "users://{user_id}" in templates
    # A template is not advertised under the concrete-URI list.
    assert _list_resources(app) == {}


def test_static_resource_read_returns_text_contents():
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app/settings",
        mcp_description="The application settings",
    )
    async def settings() -> dict:
        return {"debug": False, "name": "veloce"}

    out = _read_resource(app, "config://app/settings")
    assert "error" not in out
    contents = out["result"]["contents"]
    assert len(contents) == 1
    entry = contents[0]
    assert entry["uri"] == "config://app/settings"
    assert orjson.loads(entry["text"]) == {"debug": False, "name": "veloce"}


def test_template_resource_read_invokes_handler_with_path_param():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{user_id}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        # The value arrives coerced to int, exactly as on the HTTP path.
        return {"id": user_id, "doubled": user_id * 2}

    out = _read_resource(app, "users://21")
    assert "error" not in out
    entry = out["result"]["contents"][0]
    assert entry["uri"] == "users://21"
    assert orjson.loads(entry["text"]) == {"id": 21, "doubled": 42}


def test_resource_read_unknown_uri_is_resource_not_found():
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app/settings",
        mcp_description="Settings",
    )
    async def settings() -> dict:
        return {}

    out = _read_resource(app, "config://does/not/exist")
    assert out["error"]["code"] == RESOURCE_NOT_FOUND


def test_resource_read_route_404_is_resource_not_found():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{user_id}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        raise HTTPException(status_code=404, detail="no such user")

    out = _read_resource(app, "users://7")
    assert out["error"]["code"] == RESOURCE_NOT_FOUND


def test_resource_read_template_coercion_failure_is_invalid_params():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{user_id}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        return {"id": user_id}

    # `abc` cannot coerce to the `user_id: int` path parameter.
    out = _read_resource(app, "users://abc")
    assert out["error"]["code"] == INVALID_PARAMS


def test_resource_read_runs_route_dependency_guard():
    app = Veloce(openapi_url=None)

    def deny() -> None:
        raise HTTPException(status_code=403, detail="forbidden")

    @app.get(
        "/secret",
        dependencies=[Depends(deny)],
        expose_as_mcp_resource=True,
        mcp_resource_uri="secret://data",
        mcp_description="Guarded data",
    )
    async def secret() -> dict:
        return {"top": "secret"}

    out = _read_resource(app, "secret://data")
    # The guard runs on the resource read, so the read fails rather than
    # returning the protected body.
    assert "error" in out
    assert "result" not in out


def test_initialize_advertises_resources_when_present():
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="Settings",
    )
    async def settings() -> dict:
        return {}

    caps = _initialize(app, {})["result"]["capabilities"]
    # `listChanged` is true on a stateful connection whether or not subscriptions
    # are on: a handler can narrow this connection's resource listing with
    # `MCPContext.hide`, and the client is told so it fetches the list again.
    # `subscribe` additionally needs the subscription machinery.
    assert caps["resources"] == {"subscribe": False, "listChanged": True}


def test_initialize_omits_resources_capability_when_none():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    caps = _initialize(app, {})["result"]["capabilities"]
    assert "resources" not in caps


def test_initialize_advertises_subscriptions_when_enabled():
    caps = _initialize(_subscriptions_app(), {})["result"]["capabilities"]
    assert caps["resources"] == {"subscribe": True, "listChanged": True}


def test_subscribe_then_resource_updated_reaches_subscriber():
    app = _subscriptions_app()

    # A tool that signals a change to the subscribed resource mid-connection, so
    # the resulting `notifications/resources/updated` is written on the same loop.
    @app.mcp_tool(description="Mark settings changed")
    async def touch() -> str:
        await server.notify_resource_updated("config://app")
        return "ok"

    server = MCPServer(app)
    pipe = Pipe(server)
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/subscribe",
            "params": {"uri": "config://app"},
        }
    )
    pipe.feed({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "touch"}})
    out = asyncio.run(pipe.run())

    updates = [m for m in out if m.get("method") == "notifications/resources/updated"]
    assert len(updates) == 1
    assert updates[0]["params"] == {"uri": "config://app"}


def test_resource_updated_skips_unsubscribed_uri():
    app = _subscriptions_app()

    @app.mcp_tool(description="Mark a different resource changed")
    async def touch_other() -> str:
        await server.notify_resource_updated("config://other")
        return "ok"

    server = MCPServer(app)
    pipe = Pipe(server)
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/subscribe",
            "params": {"uri": "config://app"},
        }
    )
    pipe.feed(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "touch_other"}}
    )
    out = asyncio.run(pipe.run())

    assert not [m for m in out if m.get("method") == "notifications/resources/updated"]


def test_unsubscribe_stops_resource_updates():
    app = _subscriptions_app()

    @app.mcp_tool(description="Mark settings changed")
    async def touch() -> str:
        await server.notify_resource_updated("config://app")
        return "ok"

    server = MCPServer(app)
    pipe = Pipe(server)
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/subscribe",
            "params": {"uri": "config://app"},
        }
    )
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/unsubscribe",
            "params": {"uri": "config://app"},
        }
    )
    pipe.feed({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "touch"}})
    out = asyncio.run(pipe.run())

    assert not [m for m in out if m.get("method") == "notifications/resources/updated"]


def test_list_changed_reaches_open_connection():
    app = _subscriptions_app()

    @app.mcp_tool(description="Announce a new resource")
    async def announce() -> str:
        await server.notify_resources_list_changed()
        return "ok"

    server = MCPServer(app)
    pipe = Pipe(server)
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "announce"}})
    out = asyncio.run(pipe.run())

    changed = [m for m in out if m.get("method") == "notifications/resources/list_changed"]
    assert len(changed) == 1
    assert "params" not in changed[0]


def test_subscribe_returns_empty_result():
    pipe = Pipe(_server(_subscriptions_app()))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/subscribe",
            "params": {"uri": "config://app"},
        }
    )
    out = asyncio.run(pipe.run())[0]
    assert out["result"] == {}


def test_subscribe_rejects_missing_uri():
    pipe = Pipe(_server(_subscriptions_app()))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "resources/subscribe", "params": {}})
    out = asyncio.run(pipe.run())[0]
    assert out["error"]["code"] == INVALID_PARAMS


def test_subscribe_unknown_when_disabled():
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="Settings",
    )
    async def settings() -> dict:
        return {}

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/subscribe",
            "params": {"uri": "config://app"},
        }
    )
    out = asyncio.run(pipe.run())[0]
    # Not advertised, so the method is unknown when the feature is off.
    assert out["error"]["code"] == METHOD_NOT_FOUND


def test_notify_is_inert_when_disabled():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Touch")
    async def touch() -> str:
        await server.notify_resource_updated("config://app")
        await server.notify_resources_list_changed()
        return "ok"

    server = MCPServer(app)
    pipe = Pipe(server)
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "touch"}})
    out = asyncio.run(pipe.run())
    assert not [m for m in out if m.get("method", "").startswith("notifications/resources/")]


def test_subscribe_on_stateless_request_is_invalid():
    # The HTTP transport dispatches without a session, so a subscribe there is an
    # invalid request (subscriptions are per-connection state).
    server = MCPServer(_subscriptions_app())
    out = asyncio.run(
        server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/subscribe",
                "params": {"uri": "config://app"},
            }
        )
    )
    assert out["error"]["code"] == INVALID_PARAMS


def test_concurrent_streams_on_one_session_each_receive_updates():
    # Two SSE streams may run concurrently under one session id; each registers
    # its own sink, so a resource update must reach both, and one stream closing
    # must not silence the other.

    registry = ConnectionRegistry()
    session = MCPSession()
    session.subscriptions.add("config://app")

    received_a: list[dict] = []
    received_b: list[dict] = []

    async def sink_a(message: dict) -> None:
        received_a.append(message)

    async def sink_b(message: dict) -> None:
        received_b.append(message)

    token_a = registry.add(session, sink_a)
    token_b = registry.add(session, sink_b)

    asyncio.run(registry.notify_updated("config://app"))
    assert len(received_a) == 1
    assert len(received_b) == 1

    # The first stream closes; the second must keep receiving.
    registry.remove(token_a)
    asyncio.run(registry.notify_updated("config://app"))
    assert len(received_a) == 1
    assert len(received_b) == 2

    # Token reuse is harmless and the surviving stream is unaffected.
    registry.remove(token_a)
    registry.remove(token_b)
    asyncio.run(registry.notify_updated("config://app"))
    assert len(received_b) == 2


def test_remove_session_drops_all_of_a_sessions_streams():

    registry = ConnectionRegistry()
    session = MCPSession()
    session.subscriptions.add("config://app")

    received: list[dict] = []

    async def sink(message: dict) -> None:
        received.append(message)

    registry.add(session, sink)
    registry.add(session, sink)
    registry.remove_session(session)

    asyncio.run(registry.notify_updated("config://app"))
    assert received == []


def test_resource_on_mutating_route_is_rejected():
    app = Veloce(openapi_url=None)

    @app.post(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="Settings",
    )
    async def settings() -> dict:
        return {}

    with pytest.raises(ValueError, match="read-only"):
        _server(app)


def test_resource_without_uri_is_rejected():
    app = Veloce(openapi_url=None)

    @app.get("/settings", expose_as_mcp_resource=True, mcp_description="Settings")
    async def settings() -> dict:
        return {}

    with pytest.raises(ValueError, match="mcp_resource_uri"):
        _server(app)


def test_resource_uri_template_variable_mismatch_is_rejected():
    app = Veloce(openapi_url=None)

    @app.get(
        "/users/{user_id}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://{wrong_name}",
        mcp_description="A user record",
    )
    async def user(user_id: int) -> dict:
        return {"id": user_id}

    with pytest.raises(ValueError, match="must match its path parameters"):
        _server(app)


def test_resource_missing_description_is_rejected():
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
    )
    async def settings() -> dict:
        return {}

    with pytest.raises(ValueError, match="description"):
        _server(app)


def test_duplicate_resource_uri_is_rejected():
    app = Veloce(openapi_url=None)

    @app.get(
        "/a",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="A",
    )
    async def a() -> dict:
        return {}

    @app.get(
        "/b",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="B",
    )
    async def b() -> dict:
        return {}

    with pytest.raises(ValueError, match="Duplicate MCP resource URI"):
        _server(app)


def test_resource_read_response_model_filters_fields():
    """A resource route's `response_model` filters the body the agent reads, so a
    field outside the model never leaks over a resource read."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/me",
        response_model=PublicUser,
        expose_as_mcp_resource=True,
        mcp_resource_uri="users://me",
        mcp_description="Current user",
    )
    async def me() -> dict:
        return {"id": 1, "name": "ada", "password": "s3cret"}

    out = _read_resource(app, "users://me")
    payload = orjson.loads(out["result"]["contents"][0]["text"])
    assert payload == {"id": 1, "name": "ada"}
    assert "password" not in payload


def test_resource_read_binary_returns_blob():
    app = Veloce(openapi_url=None)
    png = b"\x89PNG\r\n\x1a\n\x00\x00binary"

    @app.get(
        "/logo",
        expose_as_mcp_resource=True,
        mcp_resource_uri="assets://logo.png",
        mcp_description="The logo image",
    )
    async def logo() -> Response:
        return Response(body=png, content_type="image/png")

    out = _read_resource(app, "assets://logo.png")
    entry = out["result"]["contents"][0]
    assert entry["mimeType"] == "image/png"
    assert "text" not in entry
    assert base64.b64decode(entry["blob"]) == png
