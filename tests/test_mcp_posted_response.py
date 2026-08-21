"""A JSON-RPC response POSTed to the HTTP transport is accepted, not rejected.

The Streamable HTTP transport groups responses with notifications: a message
that needs no reply is answered `202 Accepted` with no body. A response carries
an id and a result or an error instead of a method, which is how it is told apart
from a request — and from genuinely malformed input, which is still refused.

This is how a client answers a server-initiated request. The stdio transport
resolves such a reply before dispatch; the HTTP transport had no equivalent, so
every response-shaped POST was misread as garbage.
"""

from __future__ import annotations

import pytest

from veloce import TestClient, Veloce

_INVALID_REQUEST = -32600


def _client() -> TestClient:
    app = Veloce(title="Posted", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", path="/mcp")
    return TestClient(app)


def _post(client: TestClient, payload: dict):
    return client.post(
        "/mcp",
        json=payload,
        headers={"accept": "application/json", "content-type": "application/json"},
    )


# ── A message needing no reply ───────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        ({"jsonrpc": "2.0", "id": 99, "result": {"ok": True}}, "result"),
        ({"jsonrpc": "2.0", "id": 99, "error": {"code": -1, "message": "no"}}, "error"),
        ({"jsonrpc": "2.0", "id": "str-id", "result": {}}, "string id"),
        ({"jsonrpc": "2.0", "id": 0, "result": {}}, "falsy id"),
    ],
)
def test_a_posted_response_is_accepted_with_no_body(payload: dict, label: str):
    response = _post(_client(), payload)
    assert response.status_code == 202
    assert response.body == b""


def test_a_notification_is_still_accepted_the_same_way():
    response = _post(_client(), {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code == 202
    assert response.body == b""


def test_a_response_carrying_an_unknown_id_is_still_accepted():
    """Accepting is what 202 means; there is no pending request to match here."""
    response = _post(_client(), {"jsonrpc": "2.0", "id": 123456, "result": {}})
    assert response.status_code == 202


# ── Malformed input is still refused ─────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        ({"jsonrpc": "2.0", "id": 99}, "id but neither method nor result"),
        ({"jsonrpc": "2.0"}, "nothing at all"),
        ({"id": 99, "result": {}}, "no jsonrpc version"),
        ({"jsonrpc": "1.0", "id": 99, "result": {}}, "wrong jsonrpc version"),
        ({"jsonrpc": "2.0", "method": 42, "id": 1}, "non-string method"),
    ],
)
def test_something_that_is_neither_a_request_nor_a_response_is_refused(payload: dict, label: str):
    response = _post(_client(), payload)
    assert response.status_code == 200
    assert response.json()["error"]["code"] == _INVALID_REQUEST


# ── Requests are unaffected ──────────────────────────────────────────


def test_a_real_request_is_still_answered():
    response = _post(_client(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response.status_code == 200
    assert [t["name"] for t in response.json()["result"]["tools"]] == ["add"]


def test_a_tool_call_is_still_answered():
    response = _post(
        _client(),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
        },
    )
    assert response.json()["result"]["content"][0]["text"] == "5"


# ── The dispatcher itself ────────────────────────────────────────────


async def test_the_dispatcher_returns_nothing_for_a_response():
    """`None` is what tells a transport there is nothing to send back."""
    from veloce.contrib.mcp.server import MCPServer
    from veloce.contrib.mcp.session import MCPSession

    app = Veloce(title="Dispatch", openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    server = MCPServer(app)
    assert (
        await server.handle_message({"jsonrpc": "2.0", "id": 5, "result": {}}, MCPSession()) is None
    )


async def test_stdio_still_resolves_a_reply_before_dispatch():
    """stdio owns pending requests, so it must keep intercepting replies itself."""
    import inspect

    from veloce.contrib.mcp.transports import stdio

    source = inspect.getsource(stdio)
    assert '"method" not in message' in source
    assert "_resolve_reply" in source
