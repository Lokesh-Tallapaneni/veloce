"""A tool result's text is content, not a wire document — and is rendered as such.

`_stringify` turns a tool's return value into the text of a content block. It
calls orjson directly rather than going through the app's JSON provider or its
configured rendering options, so an app that sets `JSON_SORT_KEYS` gets sorted
keys in the JSON-RPC envelope and unsorted keys in the result inside it.

That was raised as an inconsistency, and both halves of it were examined:

*The custom provider is declined on correctness.* The text of a content block is
what the model reads; the envelope carrying it is already encoded through the
provider (see `test_mcp_json_encoder_agreement`). Routing this through it as well
would apply an app's dialect twice to one reply and stamp it into the model's
input — `{"dialect":"custom", ...}` inside the text the tool returned.

*The rendering options are declined on cost.* Making `_stringify` app-aware means
asking which app is handling this call, and that is a `contextvar` read: 99 ns
measured, against a 424 ns baseline for rendering a small result. The full lookup
came to +190 ns, or +45%, on every tool result — reproducible across three
interleaved rounds. A cached attribute did not help; the contextvar read is the
floor. Paying that on every tool call to sort the keys of a string the model reads
is the wrong trade.

These tests pin the resulting behaviour down so a future change has to argue
against the measurement rather than rediscover it.
"""

from __future__ import annotations

import json

import pytest

from veloce import MCPContext, Veloce
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
    """Stamps every object it encodes, so reaching it is unmistakable."""

    def dumps(self, obj, **kwargs):
        if isinstance(obj, dict):
            obj = {"dialect": "custom", **obj}
        return super().dumps(obj, **kwargs)


def _app(**config) -> Veloce:
    app = Veloce(title="ResultText", version="1.0.0", openapi_url=None)
    app.config.update(config)

    @app.mcp_tool(description="Report an unsorted mapping")
    async def report() -> dict:
        return {"b": 1, "a": 2}

    @app.mcp_tool(description="Report a nested mapping")
    async def nested() -> dict:
        return {"z": {"d": 1, "c": 2}, "y": 3}

    @app.mcp_tool(description="Report a value needing the fallback encoder")
    async def exotic() -> dict:
        from decimal import Decimal

        return {"b": Decimal("0.25"), "a": {1, 2}}

    app.mount_mcp(transport="http", path="/mcp")
    return app


def _text(app: Veloce, tool: str = "report") -> str:
    client = app.test_client()
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}},
        headers={"Accept": "application/json"},
    )
    return response.json()["result"]["content"][0]["text"]


# ── the rendering options reach the text ─────────────────────────────


def test_sort_keys_does_not_reach_the_result_text():
    """Declined on cost: knowing the app is a contextvar read on every result."""
    assert _text(_app(JSON_SORT_KEYS=True)) == '{"b":1,"a":2}'


def test_without_sort_keys_the_order_is_unchanged():
    assert _text(_app()) == '{"b":1,"a":2}'


def test_sort_keys_does_not_reach_a_nested_mapping():
    assert _text(_app(JSON_SORT_KEYS=True), "nested") == '{"z":{"d":1,"c":2},"y":3}'


def test_the_envelope_sorts_and_the_text_does_not():
    """The accepted asymmetry: the envelope is a wire document, the text is not."""
    app = _app(JSON_SORT_KEYS=True)
    client = app.test_client()
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "report"}},
        headers={"Accept": "application/json"},
    )
    body = response.text
    assert body.index('"id"') < body.index('"jsonrpc"')
    text = response.json()["result"]["content"][0]["text"]
    assert text.index('"b"') < text.index('"a"')


def test_the_fallback_encoder_applies_whatever_the_app_configured():
    """MCP's own `default=` is the part that must reach every result."""
    rendered = json.loads(_text(_app(JSON_SORT_KEYS=True), "exotic"))
    assert rendered["b"] == 0.25
    assert sorted(rendered["a"]) == [1, 2]


def test_the_fallback_encoder_still_applies_without_sorting():
    rendered = json.loads(_text(_app(), "exotic"))
    assert rendered["b"] == 0.25


# ── a custom provider deliberately does not reach it ─────────────────


def test_a_custom_provider_does_not_stamp_the_result_text():
    """It would apply the dialect twice and put it in the model's input."""
    app = Veloce(title="Shouty", version="1.0.0", openapi_url=None)
    app.json_provider_class = ShoutingProvider

    @app.mcp_tool(description="Report a mapping")
    async def report() -> dict:
        return {"b": 1, "a": 2}

    app.mount_mcp(transport="http", path="/mcp")
    assert _text(app) == '{"b":1,"a":2}'


def test_a_custom_provider_still_stamps_the_envelope():
    """The half that should reach it, unchanged."""
    app = Veloce(title="Shouty", version="1.0.0", openapi_url=None)
    app.json_provider_class = ShoutingProvider

    @app.mcp_tool(description="Report a mapping")
    async def report() -> dict:
        return {"b": 1, "a": 2}

    app.mount_mcp(transport="http", path="/mcp")
    client = app.test_client()
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "report"}},
        headers={"Accept": "application/json"},
    )
    assert response.json()["dialect"] == "custom"


def test_a_custom_provider_without_the_options_attribute_is_tolerated():
    """A provider need not derive from the default one."""
    from veloce.json_provider import JSONProvider

    class Bare(JSONProvider):
        def dumps(self, obj, **kwargs):
            import orjson

            return orjson.dumps(obj)

        def loads(self, data):
            import orjson

            return orjson.loads(data)

    app = Veloce(title="Bare", version="1.0.0", openapi_url=None)
    app.json_provider_class = Bare

    @app.mcp_tool(description="Report a mapping")
    async def report() -> dict:
        return {"b": 1, "a": 2}

    app.mount_mcp(transport="http", path="/mcp")
    assert _text(app) == '{"b":1,"a":2}'


# ── the other result shapes are untouched ────────────────────────────


@pytest.mark.parametrize("sort", [True, False])
def test_a_string_result_is_passed_through(sort):
    app = Veloce(title="Str", version="1.0.0", openapi_url=None)
    app.config["JSON_SORT_KEYS"] = sort

    @app.mcp_tool(description="Report a string")
    async def report() -> str:
        return "b then a"

    app.mount_mcp(transport="http", path="/mcp")
    assert _text(app) == "b then a"


@pytest.mark.parametrize("sort", [True, False])
def test_a_bytes_result_is_still_text(sort):
    """The one deliberate difference from the HTTP encoder stays."""
    app = Veloce(title="Bytes", version="1.0.0", openapi_url=None)
    app.config["JSON_SORT_KEYS"] = sort

    @app.mcp_tool(description="Report bytes")
    async def report() -> bytes:
        return b"raw text"

    app.mount_mcp(transport="http", path="/mcp")
    assert _text(app) == "raw text"


def test_a_list_result_is_rendered():
    assert json.loads(_text(_app(JSON_SORT_KEYS=True), "report")) == {"a": 2, "b": 1}


# ── outside a request there is no app to ask ─────────────────────────


def test_stringify_works_with_no_app_in_context():
    """The stdio path and any direct caller must not depend on one."""
    from veloce.contrib.mcp._helpers import _stringify

    assert _stringify({"b": 1, "a": 2}) == '{"b":1,"a":2}'


# ── the context-carrying tool path agrees ────────────────────────────


def test_a_tool_taking_a_context_renders_the_same_way():
    app = Veloce(title="Ctx", version="1.0.0", openapi_url=None)
    app.config["JSON_SORT_KEYS"] = True
    # Same rendering as the plain path, sorted or not.

    @app.mcp_tool(description="Report through a context")
    async def report(ctx: MCPContext) -> dict:
        return {"b": 1, "a": 2}

    app.mount_mcp(transport="http", path="/mcp")
    assert _text(app) == '{"b":1,"a":2}'
