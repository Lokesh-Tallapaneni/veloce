"""JSON-RPC framing rules, on both transports.

Two distinctions the dispatcher was not making:

- `-32700 Parse error` is for text that could not be read as JSON at all;
  `-32600 Invalid Request` is for JSON that read fine but is not a Request
  object. Answering both the same way told a client nothing about which mistake
  it had made — and a batch array, which the revisions this server speaks do not
  carry, looked like corrupt input rather than an unsupported shape.
- MCP, unlike base JSON-RPC, forbids a null request id. A request carries a
  string or integer id, or omits the key to be a notification; a present-but-null
  id is neither.
"""

from __future__ import annotations

import pytest

from veloce import TestClient, Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.transports.stdio import StdioTransport

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600


def _client() -> TestClient:
    app = Veloce(title="Framing", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", path="/mcp")
    return TestClient(app)


def _send(raw: bytes):
    return _client().post(
        "/mcp",
        content=raw,
        headers={"accept": "application/json", "content-type": "application/json"},
    )


# ── Which failure it was ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]', id="a batch array"),
        pytest.param(b'"just a string"', id="a bare string"),
        pytest.param(b"42", id="a bare number"),
        pytest.param(b"true", id="a bare boolean"),
        pytest.param(b"null", id="a bare null"),
    ],
)
def test_json_that_is_not_a_request_object_is_an_invalid_request(raw: bytes):
    response = _send(raw)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == _INVALID_REQUEST


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"{not valid json", id="unbalanced brace"),
        pytest.param(b'{"jsonrpc": ', id="truncated"),
        pytest.param(b"", id="empty body"),
    ],
)
def test_text_that_is_not_json_is_a_parse_error(raw: bytes):
    response = _send(raw)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == _PARSE_ERROR


def test_the_two_failures_are_told_apart():
    """The point of the fix: one code per distinct mistake."""
    shape = _send(b'"a string"').json()["error"]
    unreadable = _send(b"{not json").json()["error"]
    assert shape["code"] != unreadable["code"]
    assert shape["message"] == "Invalid Request"
    assert unreadable["message"] == "Parse error"


# ── A null id ────────────────────────────────────────────────────────


def _post(payload: dict):
    return _client().post(
        "/mcp",
        json=payload,
        headers={"accept": "application/json", "content-type": "application/json"},
    )


def test_a_request_with_a_null_id_is_refused():
    response = _post({"jsonrpc": "2.0", "id": None, "method": "ping", "params": {}})
    assert response.json()["error"]["code"] == _INVALID_REQUEST
    assert "null" in response.json()["error"]["message"]


def test_a_notification_omitting_the_id_key_is_still_accepted():
    """Omitting the key is how a notification says it wants no reply."""
    response = _post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code == 202


@pytest.mark.parametrize("ident", [1, 0, -1, "string-id", "0"])
def test_a_real_id_is_still_answered(ident):
    """`0` and `""` are falsy but perfectly valid ids."""
    response = _post({"jsonrpc": "2.0", "id": ident, "method": "ping", "params": {}})
    assert response.json()["id"] == ident
    assert response.json()["result"] == {}


# ── The stdio transport agrees ───────────────────────────────────────


async def _stdio_reply(raw: str) -> dict:
    """Feed one line to the stdio transport and return what it writes back."""

    app = Veloce(title="Stdio", openapi_url=None)

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    async def read_line() -> bytes | None:
        return None

    async def write_line(payload: bytes) -> None:
        return None

    transport = StdioTransport(MCPServer(app), read_line, write_line)
    # The loop decodes first and routes on the result: a shape failure is
    # answered without ever reaching dispatch, so that is what this reproduces.
    message, error = transport._decode(raw)
    if error is not None:
        return error
    if message is None or "method" not in message:
        return None
    return await transport.server.handle_message(message, MCPSession())


async def test_stdio_reports_a_shape_failure_the_same_way():
    reply = await _stdio_reply('"just a string"')
    assert reply["error"]["code"] == _INVALID_REQUEST


async def test_stdio_reports_a_batch_the_same_way():
    reply = await _stdio_reply('[{"jsonrpc":"2.0","id":1,"method":"ping"}]')
    assert reply["error"]["code"] == _INVALID_REQUEST


async def test_stdio_still_reports_unreadable_text_as_a_parse_error():
    reply = await _stdio_reply("{not json")
    assert reply["error"]["code"] == _PARSE_ERROR


async def test_stdio_refuses_a_null_id_too():
    reply = await _stdio_reply('{"jsonrpc":"2.0","id":null,"method":"ping","params":{}}')
    assert reply["error"]["code"] == _INVALID_REQUEST


async def test_stdio_still_answers_a_real_request():
    reply = await _stdio_reply('{"jsonrpc":"2.0","id":7,"method":"ping","params":{}}')
    assert reply["id"] == 7
    assert reply["result"] == {}
