"""Browser-shaped middleware stands down for a replayed MCP call.

`Request.is_mcp` documents the contract in its own docstring — "authentication
middleware that checks a browser credential ... should return early on these" —
and no shipped middleware honoured it. The consequence was not cosmetic: a
single `TrustedHostMiddleware`, which is ordinary hardening, made **every**
route-backed MCP tool fail with `Invalid host header`, because the replayed
request is synthesised to run a route for an agent and carries no `Host`. CSRF
did the same to every write tool, and the HTTPS upgrade handed agents a 308 to
nowhere.

The only escape hatch was per-route `exclude_middleware`, and a route's
exclusion list is shared by both doors — so opting a tool out of CSRF also took
Host validation away from the humans using the same path. A route could not
both serve an agent and enforce a Host allow-list.

Skipping these is safe because the transport request itself (`POST /mcp`) went
through the middleware stack already, and the agent is authenticated by the
transport: a different credential, checked elsewhere. What must not change is
the HTTP door, so every test here has a paired assertion that the real
protection still fires.
"""

from __future__ import annotations

from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.middleware.csrf import CSRFMiddleware
from veloce.middleware.security import HTTPSRedirectMiddleware, TrustedHostMiddleware
from veloce.testclient import TestClient


def _app(*middleware) -> Veloce:
    app = Veloce(title="Dual", openapi_url=None)
    app.config["SECRET_KEY"] = "test-key"
    for item in middleware:
        app.add_middleware(item)

    @app.get("/orders", expose_as_mcp_tool=True, mcp_description="List orders")
    async def orders():
        return {"orders": ["ORD-1"]}

    @app.post("/orders/{oid}/cancel", expose_as_mcp_tool=True, mcp_description="Cancel an order")
    async def cancel(oid: str):
        return {"cancelled": oid}

    return app


async def _tool(app: Veloce, name: str, arguments: dict | None = None) -> dict:
    return await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        MCPSession(),
    )


def _text(response: dict) -> str:
    return response["result"]["content"][0]["text"]


# ── Host validation ──────────────────────────────────────────────────


async def test_a_host_allow_list_does_not_break_a_read_tool():
    """The defect: this failed with `Invalid host header`."""
    app = _app(TrustedHostMiddleware(allowed_hosts=["example.com"]))
    assert _text(await _tool(app, "orders")) == '{"orders":["ORD-1"]}'


async def test_a_host_allow_list_does_not_break_a_write_tool():
    app = _app(TrustedHostMiddleware(allowed_hosts=["example.com"]))
    assert _text(await _tool(app, "cancel", {"oid": "ORD-1"})) == '{"cancelled":"ORD-1"}'


def test_the_http_door_still_refuses_a_bad_host():
    """Standing down for the agent must not disarm the browser path."""
    app = _app(TrustedHostMiddleware(allowed_hosts=["example.com"]))
    with TestClient(app) as client:
        assert client.get("/orders", headers={"host": "evil.com"}).status_code == 400


def test_the_http_door_still_accepts_an_allowed_host():
    app = _app(TrustedHostMiddleware(allowed_hosts=["example.com"]))
    with TestClient(app) as client:
        assert client.get("/orders", headers={"host": "example.com"}).status_code == 200


# ── CSRF ─────────────────────────────────────────────────────────────


async def test_csrf_does_not_refuse_an_agent_write():
    """An agent has no browser and no cookie, so the token can never be there."""
    app = _app(CSRFMiddleware())
    assert _text(await _tool(app, "cancel", {"oid": "ORD-2"})) == '{"cancelled":"ORD-2"}'


def test_the_http_door_still_refuses_a_tokenless_post():
    app = _app(CSRFMiddleware())
    with TestClient(app) as client:
        assert client.post("/orders/1/cancel").status_code == 403


# ── HTTPS upgrade ────────────────────────────────────────────────────


async def test_the_https_upgrade_does_not_redirect_an_agent():
    """A replayed call is not on the wire; a 308 would be unusable."""
    app = _app(HTTPSRedirectMiddleware())
    assert _text(await _tool(app, "orders")) == '{"orders":["ORD-1"]}'


def test_the_http_door_is_still_redirected():
    app = _app(HTTPSRedirectMiddleware())
    with TestClient(app) as client:
        assert client.get("/orders", follow_redirects=False).status_code == 308


# ── All of them at once, which is the realistic configuration ────────


async def test_a_normally_hardened_app_still_serves_both_doors():
    app = _app(
        TrustedHostMiddleware(allowed_hosts=["example.com"]),
        HTTPSRedirectMiddleware(),
        CSRFMiddleware(),
    )
    assert _text(await _tool(app, "orders")) == '{"orders":["ORD-1"]}'
    assert _text(await _tool(app, "cancel", {"oid": "ORD-3"})) == '{"cancelled":"ORD-3"}'


def test_a_normally_hardened_app_keeps_every_http_protection():
    app = _app(
        TrustedHostMiddleware(allowed_hosts=["example.com"]),
        HTTPSRedirectMiddleware(),
        CSRFMiddleware(),
    )
    with TestClient(app) as client:
        assert client.get("/orders", headers={"host": "evil.com"}).status_code == 400
        assert (
            client.get(
                "/orders", headers={"host": "example.com"}, follow_redirects=False
            ).status_code
            == 308
        )
        assert (
            client.post(
                "/orders/1/cancel",
                headers={"host": "example.com", "x-forwarded-proto": "https"},
            ).status_code
            == 403
        )
