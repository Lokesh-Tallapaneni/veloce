"""An MCP protocol frame is protocol, whichever transport carries it.

A JSON-RPC envelope is the framework's own wire format. `json_provider` states
the rule for those — signed cookies, JWTs, protocol frames "are not the
application's to restyle" — and `stdio` followed it. The HTTP and SSE transports
did not: they built every reply with `JSONResponse` and every frame with
`ServerSentEvent.json`, both of which resolve the application's provider.

So an app with a custom `json_provider_class` had its keys injected into the
JSON-RPC envelope, and with `JSONIFY_PRETTYPRINT_REGULAR` every SSE frame
inflated into a dozen `data:` lines — while the same server over stdio framed
the same messages correctly. One server, two protocol dialects, chosen by
transport.

**This reverses part of an earlier decision, deliberately.** An earlier pass
(recorded as audit finding 2.22) established that the plain-JSON reply and the
SSE frame must *agree*, and pinned that by asserting both carried the app's
dialect. The agreement property was right and is kept — strengthened, in fact,
because stdio is now in it too. The direction was wrong: they agree by both
being plain protocol, not by both being restyled.

The application's dialect still reaches the application's data — a tool result's
content — which `test_mcp_result_text_rendering` covers. Envelope plain, content
in the app's dialect, is the whole rule.
"""

from __future__ import annotations

import json

import pytest

from veloce import EventSourceResponse, ServerSentEvent, Veloce
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
    """Stamps every object it encodes, so a leak into protocol is unmistakable."""

    def dumps(self, obj, **kwargs):
        if isinstance(obj, dict):
            obj = {"dialect": "custom", **obj}
        return super().dumps(obj, **kwargs)


def _app(**config):
    app = Veloce(title="EncoderAgreement", version="1.0.0", openapi_url=None)
    app.json_provider_class = ShoutingProvider
    app.config.update(config)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="http", path="/mcp")
    return app


def _client(**config):
    client = _app(**config).test_client()
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
    for line in text.splitlines():
        if line.startswith("data:") and line[5:].strip():
            return json.loads(line[5:])
    raise AssertionError(f"no data frame in: {text!r}")


# ── the envelope is protocol ─────────────────────────────────────────


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_the_envelope_does_not_carry_the_dialect(accept):
    """The defect: a custom provider injected its key into the JSON-RPC frame."""
    response = _client().post("/mcp", json=_call(), headers={"Accept": accept})
    payload = response.json() if accept == "application/json" else _sse_payload(response.text)
    assert "dialect" not in payload
    assert payload["jsonrpc"] == "2.0"


@pytest.mark.parametrize(
    ("label", "message"),
    [
        ("initialize", INITIALIZE),
        ("tools/list", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        ("ping", {"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        ("tools/call", None),
    ],
)
def test_no_successful_reply_shape_carries_the_dialect(label, message):
    response = _client().post(
        "/mcp", json=message or _call(), headers={"Accept": "application/json"}
    )
    assert "dialect" not in response.json()


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
def test_no_error_reply_shape_carries_the_dialect(label, message):
    """An error envelope is protocol too."""
    response = _client().post("/mcp", json=message, headers={"Accept": "application/json"})
    assert "dialect" not in response.json()


def test_the_legacy_transport_frames_protocol_too():
    app = Veloce(title="LegacySSE", version="1.0.0", openapi_url=None)
    app.json_provider_class = ShoutingProvider

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    app.mount_mcp(transport="sse", path="/sse")
    response = app.test_client().post("/messages", json=_call())
    assert "dialect" not in response.json()


def test_a_formatting_option_does_not_reach_the_envelope():
    """`JSONIFY_PRETTYPRINT_REGULAR` inflated every SSE frame into `data:` lines."""
    client = _client(JSONIFY_PRETTYPRINT_REGULAR=True)
    body = client.post("/mcp", json=_call(), headers={"Accept": "application/json"}).text
    assert "\n" not in body.strip()


def test_sort_keys_does_not_reach_the_envelope():
    client = _client(JSON_SORT_KEYS=True)
    body = client.post("/mcp", json=_call(), headers={"Accept": "application/json"}).text
    # Protocol order, not sorted: `jsonrpc` precedes `id`.
    assert body.index('"jsonrpc"') < body.index('"id"')


# ── the two Accept values still agree ────────────────────────────────


def test_both_accept_values_produce_the_same_envelope():
    """The property the earlier pass established; kept, with the direction fixed."""
    client = _client()
    plain = client.post("/mcp", json=_call(), headers={"Accept": "application/json"}).json()
    framed = _sse_payload(
        client.post("/mcp", json=_call(), headers={"Accept": "text/event-stream"}).text
    )
    assert plain == framed


def test_an_error_reply_agrees_across_accept_values():
    client = _client()
    message = {"jsonrpc": "2.0", "id": 1, "method": "nope"}
    plain = client.post("/mcp", json=message, headers={"Accept": "application/json"}).json()
    framed_text = client.post("/mcp", json=message, headers={"Accept": "text/event-stream"}).text
    framed = _sse_payload(framed_text) if "data:" in framed_text else json.loads(framed_text)
    assert plain == framed


def test_all_three_transports_share_one_encoder():
    """stdio was already right; the other two now call the same function."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce" / "contrib" / "mcp"
    for path in ("transports/http.py", "transports/sse.py", "transports/stdio.py"):
        assert "encode_envelope" in (root / path).read_text(encoding="utf-8"), path


# ── the reply is still correct ───────────────────────────────────────


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
def test_the_reply_is_still_correct(accept):
    response = _client().post("/mcp", json=_call(), headers={"Accept": accept})
    payload = response.json() if accept == "application/json" else _sse_payload(response.text)
    assert payload["result"]["content"][0]["text"] == "3"


def test_an_app_with_no_custom_provider_is_unchanged():
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


def test_a_user_sse_stream_still_uses_the_provider():
    """`ServerSentEvent.json` is shared; only MCP stopped using it.

    An application's own event stream is application data and must keep the
    dialect - the fix must not have taken it away from user code.
    """

    app = Veloce(openapi_url=None)
    app.json_provider_class = ShoutingProvider

    @app.get("/events")
    async def events():
        async def stream():
            yield ServerSentEvent.json({"tick": 1})

        return EventSourceResponse(stream())

    assert '"dialect":"custom"' in app.test_client().get("/events").text
