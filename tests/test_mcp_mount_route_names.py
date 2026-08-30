"""`mount_mcp` gives every transport route a name unique to its mount path.

`mount_mcp` registered its transport routes without a name, so each took its
handler's function name - fixed however many times the transport was mounted.
Mounting twice left the first mount unreachable by name, `url_for("open_stream")`
silently resolving to the second. Both transports were affected (`mcp_endpoint`
for HTTP, `open_stream` / `receive_message` for SSE). The metadata route already
guarded against a double mount; the endpoints did not.

The defect was found by sweeping `docs/guide/mcp.md`, which documents mounting
the SSE transport twice, and these tests lived in that sweep's module. They are
route-naming behaviour, not a claim about the page: someone changing how
`mount_mcp` names its routes would not have looked in a module named for guide
claims, and `grep -rl "mcp_endpoint:" tests/` returned that file alone.
"""

from __future__ import annotations

import logging

from tests._mcp import initialize
from veloce import Veloce
from veloce.testclient import TestClient

INITIALIZE = initialize()


def _tool_app() -> Veloce:
    app = Veloce(title="Guide", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def probe() -> str:
        return "ok"

    return app


# ── two mounts of one transport coexist ──────────────────────


def test_mounting_http_twice_does_not_collide(caplog):
    """The defect: the second mount stole `mcp_endpoint`."""
    app = _tool_app()
    with caplog.at_level(logging.WARNING):
        app.mount_mcp(transport="http")
        app.mount_mcp(transport="http", path="/agent/mcp")
    assert not [r for r in caplog.records if "Duplicate route name" in r.getMessage()]


def test_mounting_sse_twice_does_not_collide(caplog):
    """Exactly the two-mount example the guide documents."""
    app = _tool_app()
    with caplog.at_level(logging.WARNING):
        app.mount_mcp(transport="sse")
        app.mount_mcp(transport="sse", path="/agent/sse", message_path="/agent/messages")
    assert not [r for r in caplog.records if "Duplicate route name" in r.getMessage()]


# ── each mount keeps its own names ───────────────────────────


def test_each_http_mount_keeps_its_own_name():
    app = _tool_app()
    app.mount_mcp(transport="http")
    app.mount_mcp(transport="http", path="/agent/mcp")
    assert app.url_for("mcp_endpoint:/mcp") == "/mcp"
    assert app.url_for("mcp_endpoint:/agent/mcp") == "/agent/mcp"


def test_each_sse_mount_keeps_its_own_names():
    app = _tool_app()
    app.mount_mcp(transport="sse")
    app.mount_mcp(transport="sse", path="/agent/sse", message_path="/agent/messages")
    assert app.url_for("open_stream:/sse") == "/sse"
    assert app.url_for("open_stream:/agent/sse") == "/agent/sse"
    assert app.url_for("receive_message:/messages") == "/messages"
    assert app.url_for("receive_message:/agent/messages") == "/agent/messages"


def test_both_http_mounts_serve():
    """Names aside, both endpoints must answer."""
    app = _tool_app()
    app.mount_mcp(transport="http")
    app.mount_mcp(transport="http", path="/agent/mcp")
    client = TestClient(app)
    for path in ("/mcp", "/agent/mcp"):
        response = client.post(path, json=INITIALIZE, headers={"Accept": "application/json"})
        assert response.json()["result"]["serverInfo"]["name"] == "Guide"


def test_a_single_mount_is_still_named():
    """The common case must not lose its name."""
    app = _tool_app()
    app.mount_mcp(transport="http")
    assert app.url_for("mcp_endpoint:/mcp") == "/mcp"
