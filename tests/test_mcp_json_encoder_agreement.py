"""One MCP endpoint answers with one JSON encoder, whatever the client accepts.

The Streamable HTTP transport can answer the same `tools/call` two ways: a plain
JSON body, or the same object wrapped in an SSE frame. Which one you get is
decided by the request's `Accept` header alone.

Those two paths used to reach different encoders. The SSE frame went through the
app's configured JSON provider; the plain body went through a `JSONResponse`
shortcut that called `orjson.dumps` directly. So an app that had customised its
JSON — a sort order, a stamped envelope, a registered type encoder — got that
customisation on one of its own replies and not the other, chosen by a header the
client sets:

    plain JSON reply : {"jsonrpc":"2.0","id":1,"result":{...}}
    SSE-framed reply : data: {"dialect":"custom","jsonrpc":"2.0","id":1,"result":{...}}

That shortcut was the app-wide defect fixed as 1.1 (`JSONResponse` bypassing the
provider), and closing it closed every MCP site that funnelled into it. What was
missing was any test saying so: the dialect suite covered the HTTP surfaces and
never touched MCP, which is how one endpoint came to disagree with itself.

These tests are that coverage — every MCP reply shape, both transports, both
`Accept` values.
"""

from __future__ import annotations

import json

import pytest

from veloce import Veloce
from veloce.json_provider import DefaultJSONProvider

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}


class ShoutingProvider(DefaultJSONProvider):
    """Stamps every object it encodes, so a bypass is unmistakable."""

    def dumps(self, obj, **kwargs):
        if isinstance(obj, dict):
            obj = {"dialect": "custom", **obj}
        return super().dumps(obj, **kwargs)


def _app(**mount):
    app = Veloce(title="EncoderAgreement", version="1.0.0", openapi_url=None)
    app.json_provider_class = ShoutingProvider

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", path="/mcp", **mount)
    return app


def _client():
    client = _app().test_client()
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    return client


def _call(ident: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": ident,
        "method": "tools/call",
        "params": {"name": "add", "arguments": {"a": 1, "b": 2}},
    }


def _sse_payload(text: str) -> dict:
    """The JSON object carried by an SSE reply's `data:` line."""
    for line in text.splitlines():
        if line.startswith("data:") and line[5:].strip():
            return json.loads(line[5:])
    raise AssertionError(f"no data frame in: {text!r}")


# ── the two Accept values agree ──────────────────────────────────────


def test_both_accept_values_produce_the_same_object():
    """The defect: the header decided which encoder ran."""
    client = _client()
    plain = client.post("/mcp", json=_call(), headers={"Accept": "application/json"}).json()
    framed = _sse_payload(
        client.post("/mcp", json=_call(), headers={"Accept": "text/event-stream"}).text
    )
    assert plain == framed


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_the_dialect_reaches_a_tool_reply(accept):
    client = _client()
    response = client.post("/mcp", json=_call(), headers={"Accept": accept})
    payload = response.json() if accept == "application/json" else _sse_payload(response.text)
    assert payload["dialect"] == "custom"


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_the_reply_is_still_correct(accept):
    """Routing through the provider must not change the answer."""
    client = _client()
    response = client.post("/mcp", json=_call(), headers={"Accept": accept})
    payload = response.json() if accept == "application/json" else _sse_payload(response.text)
    assert payload["result"]["content"][0]["text"] == "3"


# ── every reply shape, not just a tool call ──────────────────────────


@pytest.mark.parametrize(
    ("label", "message"),
    [
        ("initialize", INITIALIZE),
        ("tools/list", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        ("ping", {"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        ("tools/call", None),
    ],
)
def test_every_successful_reply_shape_carries_the_dialect(label, message):
    client = _client()
    response = client.post("/mcp", json=message or _call(), headers={"Accept": "application/json"})
    assert response.json()["dialect"] == "custom"


@pytest.mark.parametrize(
    ("label", "message"),
    [
        ("unknown method", {"jsonrpc": "2.0", "id": 1, "method": "nope"}),
        ("not a request object", [1, 2]),
        (
            "unknown tool",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}},
        ),
    ],
)
def test_every_error_reply_shape_carries_the_dialect(label, message):
    """Error bodies took the same shortcut as success bodies."""
    client = _client()
    response = client.post("/mcp", json=message, headers={"Accept": "application/json"})
    assert response.json()["dialect"] == "custom"


def test_an_error_reply_agrees_across_accept_values():
    client = _client()
    message = {"jsonrpc": "2.0", "id": 1, "method": "nope"}
    plain = client.post("/mcp", json=message, headers={"Accept": "application/json"}).json()
    framed_text = client.post("/mcp", json=message, headers={"Accept": "text/event-stream"}).text
    framed = _sse_payload(framed_text) if "data:" in framed_text else json.loads(framed_text)
    assert plain == framed


# ── the legacy SSE transport uses the same encoder ───────────────────


def test_the_legacy_transport_stamps_its_error_replies():
    """Its `POST` answers errors with `JSONResponse` too."""
    app = Veloce(title="LegacySSE", version="1.0.0", openapi_url=None)
    app.json_provider_class = ShoutingProvider

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="sse", path="/sse")
    client = app.test_client()
    # No session id, so the POST is refused - through the same encoder.
    response = client.post("/messages", json=_call())
    assert response.json()["dialect"] == "custom"


# ── the default provider is unaffected ───────────────────────────────


def test_an_app_with_no_custom_provider_emits_plain_json():
    app = Veloce(title="Plain", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", path="/mcp")
    client = app.test_client()
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    payload = client.post("/mcp", json=_call(), headers={"Accept": "application/json"}).json()
    assert "dialect" not in payload
    assert payload["result"]["content"][0]["text"] == "3"


def test_json_sort_keys_reaches_an_mcp_reply():
    """A second, independent provider setting - not just the stamping one."""
    app = Veloce(title="Sorted", version="1.0.0", openapi_url=None)
    app.config["JSON_SORT_KEYS"] = True

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", path="/mcp")
    client = app.test_client()
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    body = client.post("/mcp", json=_call(), headers={"Accept": "application/json"}).text
    assert body.index('"id"') < body.index('"jsonrpc"')
