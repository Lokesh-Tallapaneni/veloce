"""One definition of "modern protocol era", used by both ends of the server.

The era was decided twice, by different rules. The transport asked what revision
the request *names* — `version >= "2026-07-28"` — and the core asked only whether
a `_meta.protocolVersion` was *present* at all.

A body naming a handshake-era revision therefore split them. The transport read
it as handshake-era and skipped `_validate_standard_headers`, the cross-check
that stops a proxy's headers and the body they label from describing different
requests. The core read the same body as modern and answered it in the modern
envelope, with `ping` removed:

    body _meta=2025-06-18, no Mcp-Method header
      -> transport: legacy, header check skipped, waved through
      -> core:      modern, so `ping` is not a method any more

The cross-check exists so a hop's two ends cannot act on different requests.
Here the server's own two ends did. The transport's rule was the documented and
correct one, so the core adopted it, and `is_modern_version` is now the single
place either end asks.
"""

from __future__ import annotations

import pytest

from veloce import MCPContext, Veloce
from veloce.contrib.mcp.server import (
    LATEST_PROTOCOL_VERSION,
    MODERN_PROTOCOL_VERSION,
    is_modern_version,
)

HANDSHAKE_ERA = "2025-06-18"


def _app() -> Veloce:
    app = Veloce(title="EraGate", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    @app.mcp_tool(description="Name the transport")
    async def where(ctx: MCPContext) -> dict:
        return {"transport": ctx.transport}

    app.mount_mcp(transport="http", path="/mcp")
    return app


def _post(client, payload: dict, headers: dict | None = None):
    return client.post(
        "/mcp",
        json=payload,
        headers={"Accept": "application/json", **(headers or {})},
    )


def _meta_call(version: str | None, method: str = "ping", params: dict | None = None) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    merged = dict(params or {})
    if version is not None:
        merged["_meta"] = {"io.modelcontextprotocol/protocolVersion": version}
    if merged:
        body["params"] = merged
    return body


# ── the shared predicate ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("version", "modern"),
    [
        (None, False),
        ("2024-11-05", False),
        ("2025-03-26", False),
        (HANDSHAKE_ERA, False),
        (MODERN_PROTOCOL_VERSION, True),
        ("2027-01-01", True),
        ("", False),
    ],
)
def test_the_predicate_reads_the_value_not_the_presence(version, modern):
    assert is_modern_version(version) is modern


def test_a_later_revision_is_modern_too():
    """String ordering of ISO dates is what makes this hold without a list."""
    assert is_modern_version("9999-12-31") is True


def test_the_transport_no_longer_defines_its_own():
    """Two definitions is what let the two ends disagree."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "veloce"
        / "contrib"
        / "mcp"
        / "transports"
        / "http.py"
    ).read_text(encoding="utf-8")
    assert "def _is_modern_version" not in source
    assert "is_modern_version" in source


def test_the_core_no_longer_gates_on_presence():
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "veloce"
        / "contrib"
        / "mcp"
        / "server.py"
    ).read_text(encoding="utf-8")
    assert "is_modern = requested_version is not None" not in source
    assert "is_modern = is_modern_version(requested_version)" in source


# ── the divergence, end to end ───────────────────────────────────────


def test_a_handshake_era_body_is_served_as_handshake_era():
    """The defect: the core removed `ping` for a body naming 2025-06-18."""
    client = _app().test_client()
    response = _post(client, _meta_call(HANDSHAKE_ERA, "ping"))
    assert response.status_code == 200
    assert "error" not in response.json()


def test_a_handshake_era_body_with_no_version_is_served_as_handshake_era():
    client = _app().test_client()
    assert "error" not in _post(client, _meta_call(None, "ping")).json()


def test_a_modern_body_still_has_no_ping():
    """The modern revision genuinely drops it; that part was right."""
    client = _app().test_client()
    response = _post(
        client,
        _meta_call(MODERN_PROTOCOL_VERSION, "ping"),
        {"MCP-Protocol-Version": MODERN_PROTOCOL_VERSION, "Mcp-Method": "ping"},
    )
    assert response.json()["error"]["code"] == -32601


def test_a_handshake_era_body_needs_no_standard_headers():
    """It never defined them, so requiring them would refuse a correct call."""
    client = _app().test_client()
    response = _post(
        client,
        _meta_call(HANDSHAKE_ERA, "tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}}),
    )
    assert response.status_code == 200
    assert "error" not in response.json()


# ── the header cross-check still guards the modern era ───────────────


def test_a_modern_body_without_the_method_header_is_refused():
    """The anti-smuggling check the skipped branch was bypassing."""
    client = _app().test_client()
    response = _post(
        client,
        _meta_call(MODERN_PROTOCOL_VERSION, "tools/call", {"name": "add", "arguments": {}}),
        {"MCP-Protocol-Version": MODERN_PROTOCOL_VERSION},
    )
    assert response.status_code >= 400 or "error" in response.json()


def test_a_method_header_disagreeing_with_the_body_is_refused():
    client = _app().test_client()
    response = _post(
        client,
        _meta_call(MODERN_PROTOCOL_VERSION, "tools/call", {"name": "add", "arguments": {}}),
        {"MCP-Protocol-Version": MODERN_PROTOCOL_VERSION, "Mcp-Method": "tools/list"},
    )
    assert response.status_code >= 400 or "error" in response.json()


def test_a_name_header_disagreeing_with_the_body_is_refused():
    client = _app().test_client()
    response = _post(
        client,
        _meta_call(
            MODERN_PROTOCOL_VERSION, "tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}}
        ),
        {
            "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "where",
        },
    )
    assert response.status_code >= 400 or "error" in response.json()


def test_a_modern_call_with_agreeing_headers_is_served():
    client = _app().test_client()
    response = _post(
        client,
        _meta_call(
            MODERN_PROTOCOL_VERSION, "tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}}
        ),
        {
            "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "add",
        },
    )
    assert response.status_code == 200
    assert "error" not in response.json()


def test_a_version_header_disagreeing_with_the_body_is_refused():
    client = _app().test_client()
    response = _post(
        client,
        _meta_call(MODERN_PROTOCOL_VERSION, "tools/list"),
        {"MCP-Protocol-Version": "2027-01-01", "Mcp-Method": "tools/list"},
    )
    assert response.status_code >= 400 or "error" in response.json()


def test_a_modern_version_header_with_a_legacy_body_still_checks_headers():
    """Either end naming a modern revision opens the gate; that is unchanged."""
    client = _app().test_client()
    response = _post(
        client,
        _meta_call(None, "tools/list"),
        {"MCP-Protocol-Version": MODERN_PROTOCOL_VERSION},
    )
    assert response.status_code >= 400 or "error" in response.json()


# ── the handshake era is otherwise untouched ─────────────────────────


def _initialize(version: str = LATEST_PROTOCOL_VERSION) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }


def test_a_handshake_client_can_still_initialize():
    client = _app().test_client()
    response = _post(client, _initialize())
    assert response.json()["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


def test_a_handshake_client_can_still_call_a_tool():
    client = _app().test_client()
    _post(client, _initialize())
    response = _post(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
        },
    )
    assert "5" in response.json()["result"]["content"][0]["text"]


def test_a_handshake_client_can_still_ping():
    client = _app().test_client()
    _post(client, _initialize())
    assert "error" not in _post(client, {"jsonrpc": "2.0", "id": 2, "method": "ping"}).json()


def test_a_modern_client_can_still_call_a_tool():
    client = _app().test_client()
    response = _post(
        client,
        _meta_call(
            MODERN_PROTOCOL_VERSION, "tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}
        ),
        {
            "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "add",
        },
    )
    assert "5" in response.json()["result"]["content"][0]["text"]
