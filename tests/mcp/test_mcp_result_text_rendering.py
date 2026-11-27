"""A tool result carries the application's JSON dialect, however it was declared.

`_stringify` renders a tool's return value into the `content[].text` of its
result. It called `orjson` directly, so the application's JSON provider reached
one kind of tool and not the other:

    HTTP  GET /status        {"dialect":"custom","ok":true,"workers":3}
    MCP   route-backed tool  {"dialect":"custom","ok":true,"workers":3}
    MCP   pure tool          {"ok":true,"workers":3}          <- no dialect

A tool exposed from a route (`expose_as_mcp_tool=True`) carried it; a tool
declared with `@app.mcp_tool` did not. Same server, same payload, two answers
decided by which decorator wrote the handler.

And the route-backed one had it only by accident: its response body is encoded
through the provider, and MCP decodes that body and re-encodes it here, so an
injected *key* survived the round trip while anything else was lost —
`JSONIFY_PRETTYPRINT_REGULAR` gave an indented HTTP body and a compact tool text
from the same handler.

**This reverses an earlier decision, deliberately.** An earlier pass declined to
route this through the provider on two grounds. The cost ground was measured
against making `_stringify` read a `contextvar` — 99ns on a 424ns render — and
that implementation is not the one used: `MCPServer` resolves the serialiser once
at construction and passes it down, so an app that configured nothing pays
nothing. The correctness ground was that stamping the text would apply the
dialect twice, once in the envelope and once in the content — and that objection
is gone, because the envelope is now plain protocol
(`test_mcp_json_encoder_agreement`).

Envelope plain, content in the app's dialect. One rule, both tool kinds.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from tests._mcp import initialize
from veloce import MCPContext, Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.json_provider import DefaultJSONProvider
from veloce.secret import Secret

INITIALIZE = initialize()


class ShoutingProvider(DefaultJSONProvider):
    """Stamps every object it encodes."""

    def dumps(self, obj, **kwargs):
        if isinstance(obj, dict):
            obj = {"dialect": "custom", **obj}
        return super().dumps(obj, **kwargs)


def _app(provider=None, **config) -> Veloce:
    app = Veloce(title="ResultText", version="1.0.0", openapi_url=None)
    if provider is not None:
        app.json_provider_class = provider
    app.config.update(config)

    @app.get("/report", expose_as_mcp_tool=True, mcp_description="Report a mapping")
    async def report() -> dict:
        return {"b": 1, "a": 2}

    @app.mcp_tool(description="Report the same mapping, tool only")
    async def pure_report() -> dict:
        return {"b": 1, "a": 2}

    @app.mcp_tool(description="Report through a context")
    async def ctx_report(ctx: MCPContext) -> dict:
        return {"b": 1, "a": 2}

    @app.mcp_tool(description="Report a value needing the fallback encoder")
    async def exotic() -> dict:
        return {"b": Decimal("0.25"), "a": {1, 2}}

    app.mount_mcp(transport="http", path="/mcp")
    return app


def _text(app: Veloce, tool: str = "pure_report") -> str:
    client = app.test_client()
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}},
        headers={"Accept": "application/json"},
    )
    return response.json()["result"]["content"][0]["text"]


TOOLS = ["report", "pure_report", "ctx_report"]


# ── both kinds of tool carry the dialect ─────────────────────────────


@pytest.mark.parametrize("tool", TOOLS)
def test_a_tool_result_carries_the_dialect(tool):
    """The defect: `pure_report` came out stock while `report` did not."""
    assert json.loads(_text(_app(ShoutingProvider), tool))["dialect"] == "custom"


def test_the_two_tool_kinds_agree():
    """The property, stated directly: how it was declared must not show."""
    app = _app(ShoutingProvider)
    assert _text(app, "report") == _text(app, "pure_report")


def test_a_tool_agrees_with_the_http_door():
    """The docstrings' own promise: one handler, both doors, same JSON."""
    app = _app(ShoutingProvider)
    http_body = app.test_client().get("/report").text
    assert _text(app, "report") == http_body


@pytest.mark.parametrize("tool", TOOLS)
def test_sort_keys_reaches_a_tool_result(tool):
    """A rendering option, not just a stamping provider."""
    assert _text(_app(None, JSON_SORT_KEYS=True), tool) == '{"a":2,"b":1}'


@pytest.mark.parametrize("tool", TOOLS)
def test_pretty_print_reaches_a_tool_result(tool):
    """The option the accidental round trip used to drop."""
    assert "\n" in _text(_app(None, JSONIFY_PRETTYPRINT_REGULAR=True), tool)


def test_pretty_print_agrees_with_the_http_door():
    app = _app(None, JSONIFY_PRETTYPRINT_REGULAR=True)
    assert _text(app, "report") == app.test_client().get("/report").text


# ── the envelope stays out of it ─────────────────────────────────────


def test_the_dialect_is_applied_once():
    """Envelope plain, content stamped - not stamped twice."""
    app = _app(ShoutingProvider)
    client = app.test_client()
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "pure_report"}},
        headers={"Accept": "application/json"},
    )
    envelope = response.json()
    assert "dialect" not in envelope
    assert json.loads(envelope["result"]["content"][0]["text"])["dialect"] == "custom"


# ── an app that configured nothing is unchanged ──────────────────────


@pytest.mark.parametrize("tool", TOOLS)
def test_a_plain_app_renders_stock_json(tool):
    assert _text(_app(None), tool) == '{"b":1,"a":2}'


def test_a_plain_app_pays_no_resolution():
    """`resolve_dumps` returns `None` when nothing is configured."""

    assert MCPServer(_app(None))._result_dumps is None


def test_a_configured_app_resolves_once():

    assert MCPServer(_app(ShoutingProvider))._result_dumps is not None


# ── MCP's own encoder rules still hold ───────────────────────────────


def test_the_fallback_encoder_still_applies():
    """A `Decimal` and a `set` must still render, dialect or not."""
    rendered = json.loads(_text(_app(None), "exotic"))
    assert rendered["b"] == 0.25
    assert sorted(rendered["a"]) == [1, 2]


def test_the_fallback_encoder_applies_under_a_provider():
    rendered = json.loads(_text(_app(ShoutingProvider), "exotic"))
    assert rendered["b"] == 0.25
    assert rendered["dialect"] == "custom"


@pytest.mark.parametrize("provider", [None, ShoutingProvider])
def test_a_string_result_is_passed_through(provider):
    """Not JSON at all - a string is the text, whatever the dialect."""
    app = Veloce(title="Str", version="1.0.0", openapi_url=None)
    if provider is not None:
        app.json_provider_class = provider

    @app.mcp_tool(description="Report a string")
    async def pure_report() -> str:
        return "b then a"

    app.mount_mcp(transport="http", path="/mcp")
    assert _text(app) == "b then a"


@pytest.mark.parametrize("provider", [None, ShoutingProvider])
def test_a_bytes_result_is_still_text(provider):
    """MCP's one deliberate difference from the HTTP encoder stays."""
    app = Veloce(title="Bytes", version="1.0.0", openapi_url=None)
    if provider is not None:
        app.json_provider_class = provider

    @app.mcp_tool(description="Report bytes")
    async def pure_report() -> bytes:
        return b"raw text"

    app.mount_mcp(transport="http", path="/mcp")
    assert _text(app) == "raw text"


def test_a_provider_that_refuses_a_value_still_refuses_it():
    """A `Secret` is refused on purpose; a provider must not smuggle it out."""

    app = Veloce(title="Sec", version="1.0.0", openapi_url=None)
    app.json_provider_class = ShoutingProvider

    @app.mcp_tool(description="Leak a secret")
    async def pure_report() -> dict:
        return {"token": Secret("hunter2")}

    app.mount_mcp(transport="http", path="/mcp")
    client = app.test_client()
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "pure_report"}},
        headers={"Accept": "application/json"},
    )
    assert "hunter2" not in response.text
