"""Standard request headers on the modern Streamable HTTP transport.

A modern POST states its protocol revision, its method, and - for the three
methods that act on a named thing - that name in headers as well as in the
JSON-RPC body. A fronting proxy routes on the headers without parsing JSON while
the server executes the body, so a request whose two halves disagree is a
smuggling primitive: the hop's two ends would act on different requests. The
server rejects it with HTTP 400 and JSON-RPC `-32020` rather than serving it.

Earlier revisions defined none of these headers, so a handshake-era request must
be entirely unaffected - which the last section pins.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.transports.http import register_http_transport
from veloce.testclient import TestClient

MODERN = "2026-07-28"
HANDSHAKE = "2025-06-18"
_META_KEY = "io.modelcontextprotocol/protocolVersion"

HEADER_MISMATCH = -32020


def _client() -> TestClient:
    app = Veloce(title="Headers", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    @app.mcp_tool(name="héllo", description="A tool with a non-ASCII name")
    async def unicode_named() -> str:
        return "ok"

    @app.mcp_prompt(description="A prompt")
    async def greet() -> str:
        return "hi"

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
    return TestClient(app)


def _body(method: str, params: dict | None = None, *, version: str | None = MODERN) -> dict:
    payload = dict(params or {})
    if version is not None:
        payload["_meta"] = {_META_KEY: version}
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": payload}


def _headers(**overrides: str | None) -> dict[str, str]:
    """The conforming header set, with named entries overridden or dropped."""
    headers = {"accept": "application/json"}
    for key, value in overrides.items():
        if value is not None:
            headers[key.replace("_", "-")] = value
    return headers


def _post(client: TestClient, body: dict, headers: dict[str, str]):
    return client.post("/mcp", json=body, headers=headers)


def _assert_rejected(response) -> None:
    assert response.status_code == 400
    assert response.json()["error"]["code"] == HEADER_MISMATCH


# ── The conforming request is served ─────────────────────────────────


def test_a_conforming_modern_call_is_served():
    client = _client()
    response = _post(
        client,
        _body("tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}}),
        _headers(mcp_protocol_version=MODERN, mcp_method="tools/call", mcp_name="add"),
    )
    assert response.status_code == 200
    assert "error" not in response.json()


def test_a_method_that_names_nothing_needs_no_name_header():
    client = _client()
    response = _post(
        client,
        _body("tools/list"),
        _headers(mcp_protocol_version=MODERN, mcp_method="tools/list"),
    )
    assert response.status_code == 200
    assert "error" not in response.json()


@pytest.mark.parametrize(
    ("method", "params", "name"),
    [
        ("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}}, "add"),
        ("resources/read", {"uri": "res://one"}, "res://one"),
        ("prompts/get", {"name": "greet"}, "greet"),
    ],
)
def test_every_name_bearing_method_accepts_its_matching_name(method, params, name):
    client = _client()
    response = _post(
        client,
        _body(method, params),
        _headers(mcp_protocol_version=MODERN, mcp_method=method, mcp_name=name),
    )
    assert response.status_code == 200


# ── A missing required header ────────────────────────────────────────


def test_a_modern_request_without_a_protocol_version_header_is_rejected():
    client = _client()
    _assert_rejected(_post(client, _body("tools/list"), _headers(mcp_method="tools/list")))


def test_a_modern_request_without_a_method_header_is_rejected():
    client = _client()
    _assert_rejected(_post(client, _body("tools/list"), _headers(mcp_protocol_version=MODERN)))


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}}),
        ("resources/read", {"uri": "res://one"}),
        ("prompts/get", {"name": "greet"}),
    ],
)
def test_a_name_bearing_method_without_a_name_header_is_rejected(method, params):
    client = _client()
    _assert_rejected(
        _post(
            client,
            _body(method, params),
            _headers(mcp_protocol_version=MODERN, mcp_method=method),
        )
    )


# ── A header that disagrees with the body ────────────────────────────


def test_a_method_header_naming_another_method_is_rejected():
    """The smuggling shape: the proxy routes a list, the server would run a call."""
    client = _client()
    _assert_rejected(
        _post(
            client,
            _body("tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}}),
            _headers(mcp_protocol_version=MODERN, mcp_method="tools/list", mcp_name="add"),
        )
    )


def test_a_name_header_naming_another_tool_is_rejected():
    client = _client()
    _assert_rejected(
        _post(
            client,
            _body("tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}}),
            _headers(
                mcp_protocol_version=MODERN, mcp_method="tools/call", mcp_name="something_else"
            ),
        )
    )


def test_a_name_header_naming_another_resource_is_rejected():
    client = _client()
    _assert_rejected(
        _post(
            client,
            _body("resources/read", {"uri": "res://one"}),
            _headers(
                mcp_protocol_version=MODERN,
                mcp_method="resources/read",
                mcp_name="res://other",
            ),
        )
    )


def test_a_protocol_version_header_disagreeing_with_the_body_is_rejected():
    client = _client()
    _assert_rejected(
        _post(
            client,
            _body("tools/list", version=HANDSHAKE),
            _headers(mcp_protocol_version=MODERN, mcp_method="tools/list"),
        )
    )


# ── The base64 sentinel for a name outside plain ASCII ───────────────


def _sentinel(value: str) -> str:
    return f"=?base64?{base64.b64encode(value.encode()).decode()}?="


def test_the_spec_example_encodes_as_the_spec_says():
    """Standard base64 with padding over UTF-8, between the two markers."""
    assert _sentinel("Hello, 世界") == "=?base64?SGVsbG8sIOS4lueVjA==?="


def test_a_non_ascii_name_matches_through_the_sentinel():
    client = _client()
    response = _post(
        client,
        _body("tools/call", {"name": "héllo", "arguments": {}}),
        _headers(
            mcp_protocol_version=MODERN,
            mcp_method="tools/call",
            mcp_name=_sentinel("héllo"),
        ),
    )
    assert response.status_code == 200


def test_a_sentinel_decoding_to_the_wrong_name_is_still_rejected():
    """Encoding is not a way around the comparison."""
    client = _client()
    _assert_rejected(
        _post(
            client,
            _body("tools/call", {"name": "héllo", "arguments": {}}),
            _headers(
                mcp_protocol_version=MODERN,
                mcp_method="tools/call",
                mcp_name=_sentinel("other"),
            ),
        )
    )


def test_a_malformed_sentinel_payload_is_rejected():
    client = _client()
    _assert_rejected(
        _post(
            client,
            _body("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}}),
            _headers(
                mcp_protocol_version=MODERN,
                mcp_method="tools/call",
                mcp_name="=?base64?not!valid!base64?=",
            ),
        )
    )


@pytest.mark.parametrize("marker", ["=?BASE64?SGk=?=", "=?Base64?SGk=?="])
def test_the_sentinel_markers_are_case_sensitive(marker):
    """An upper-case marker is not the sentinel, so it is read as a plain value."""
    client = _client()
    _assert_rejected(
        _post(
            client,
            _body("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}}),
            _headers(mcp_protocol_version=MODERN, mcp_method="tools/call", mcp_name=marker),
        )
    )


@pytest.mark.parametrize("raw", [" add", "add ", "a\x7fdd"])
def test_a_plain_name_carrying_characters_that_must_be_encoded_is_rejected(raw):
    """Whitespace at the edges and control characters have to travel encoded."""
    client = _client()
    _assert_rejected(
        _post(
            client,
            _body("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}}),
            _headers(mcp_protocol_version=MODERN, mcp_method="tools/call", mcp_name=raw),
        )
    )


# ── The handshake era is untouched ───────────────────────────────────


def test_a_handshake_request_needs_none_of_these_headers():
    """The revision that predates the headers must not be asked for them."""
    client = _client()
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"accept": "application/json"},
    )
    assert response.status_code == 200
    assert "error" not in response.json()


def test_a_handshake_request_naming_its_own_revision_is_still_untouched():
    client = _client()
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"accept": "application/json", "mcp-protocol-version": HANDSHAKE},
    )
    assert response.status_code == 200
    assert "error" not in response.json()


def test_a_handshake_revision_named_in_the_body_is_not_held_to_modern_headers():
    """The gate reads the revision each half names, not the presence of `_meta`.

    A client that spells `2025-06-18` in both the header and `_meta` names a
    revision that defined none of these headers, so demanding them refused a
    request that was correct in both places.
    """
    client = _client()
    response = _post(
        client,
        _body("tools/list", version=HANDSHAKE),
        _headers(mcp_protocol_version=HANDSHAKE),
    )
    assert response.status_code == 200
    assert "error" not in response.json()


def test_a_handshake_revision_in_the_body_alone_is_untouched():
    client = _client()
    response = _post(client, _body("tools/list", version=HANDSHAKE), _headers())
    assert response.status_code == 200
    assert "error" not in response.json()


def test_the_two_doors_agree_on_a_handshake_revision():
    """The same message must not be served on one transport and refused on the other."""
    app = Veloce(title="Headers", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    server = MCPServer(app)
    register_http_transport(app, server)

    message = _body("tools/list", version=HANDSHAKE)
    over_http = TestClient(app).post(
        "/mcp", json=message, headers=_headers(mcp_protocol_version=HANDSHAKE)
    )
    direct = asyncio.run(server.handle_message(message))

    assert over_http.status_code == 200
    assert "error" not in over_http.json()
    assert "error" not in direct
    assert over_http.json()["result"] == direct["result"]


def test_a_handshake_call_needs_no_name_header():
    client = _client()
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
        },
        headers={"accept": "application/json", "mcp-protocol-version": HANDSHAKE},
    )
    assert response.status_code == 200
    assert "error" not in response.json()


# ── The gate opens on either half ────────────────────────────────────


def test_a_modern_header_alone_still_requires_the_rest():
    """A header a proxy trusted is exactly what must not go unchecked."""
    client = _client()
    _assert_rejected(
        _post(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            _headers(mcp_protocol_version=MODERN),
        )
    )


def test_a_modern_body_alone_still_requires_the_headers():
    client = _client()
    _assert_rejected(_post(client, _body("tools/list"), _headers()))


def test_a_notification_is_held_to_the_same_rule():
    """A one-way message is routed on its headers too."""
    client = _client()
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {"_meta": {_META_KEY: MODERN}},
        },
        headers={"accept": "application/json", "mcp-protocol-version": MODERN},
    )
    _assert_rejected(response)


def test_the_rejection_echoes_the_request_id():
    """A client correlates the refusal with the call it made."""
    client = _client()
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 42, "method": "tools/list", "params": {}},
        headers={"accept": "application/json", "mcp-protocol-version": MODERN},
    )
    assert response.json()["id"] == 42
