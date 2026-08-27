"""A `401` challenge points at a route the server actually serves.

Both MCP transports refuse an unauthenticated request with the same RFC 6750
header, built by the same helper:

    WWW-Authenticate: Bearer error="invalid_token",
                      resource_metadata="/.well-known/oauth-protected-resource"

That header is an instruction. A client reads it, fetches the named document, and
learns which authorization server to get a token from — that is the entire point
of sending it. Only `register_http_transport` registered the route, so on an SSE
mount the instruction led to a 404 and the client had nowhere to go.

`auth.py`'s own module docstring promised "each with a `WWW-Authenticate`
challenge pointing at the server's RFC 9728 protected-resource metadata". It was
true on one transport.

The registration is shared now. The path is fixed by the RFC, so an app mounting
both transports registers it once.
"""

from __future__ import annotations

import pytest

from tests._mcp import auth
from tests._mcp_source import calls, defines, tree
from veloce import Veloce
from veloce.contrib.mcp.auth import PROTECTED_RESOURCE_METADATA_PATH, MCPAuth

#: (transport, mount path, the path an unauthenticated POST goes to)
TRANSPORTS = [
    ("http", "/mcp", "/mcp"),
    ("sse", "/sse", "/messages"),
]


def _auth() -> MCPAuth:
    return auth()


def _app(transport: str, path: str, **mount) -> Veloce:
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def probe() -> dict:
        return {}

    app.mount_mcp(transport=transport, path=path, **mount)
    return app


def _ping() -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "ping"}


# ── the challenge and the route agree ────────────────────────────────


@pytest.mark.parametrize(("transport", "path", "post_to"), TRANSPORTS)
def test_the_metadata_route_is_served(transport, path, post_to):
    """The defect: this was a 404 on the SSE transport."""
    client = _app(transport, path, auth=_auth()).test_client()
    assert client.get(PROTECTED_RESOURCE_METADATA_PATH).status_code == 200


@pytest.mark.parametrize(("transport", "path", "post_to"), TRANSPORTS)
def test_the_challenge_names_that_route(transport, path, post_to):
    client = _app(transport, path, auth=_auth()).test_client()
    response = client.post(post_to, json=_ping(), headers={"Accept": "application/json"})
    assert response.status_code == 401
    assert PROTECTED_RESOURCE_METADATA_PATH in response.headers["WWW-Authenticate"]


@pytest.mark.parametrize(("transport", "path", "post_to"), TRANSPORTS)
def test_following_the_challenge_reaches_the_document(transport, path, post_to):
    """The property that was missing: the instruction can be followed."""
    client = _app(transport, path, auth=_auth()).test_client()
    challenge = client.post(post_to, json=_ping(), headers={"Accept": "application/json"}).headers[
        "WWW-Authenticate"
    ]
    named = challenge.split('resource_metadata="')[1].split('"')[0]
    assert client.get(named).status_code == 200


@pytest.mark.parametrize(("transport", "path", "post_to"), TRANSPORTS)
def test_the_document_names_the_authorization_server(transport, path, post_to):
    """A metadata document that resolves but says nothing is no better."""
    client = _app(transport, path, auth=_auth()).test_client()
    document = client.get(PROTECTED_RESOURCE_METADATA_PATH).json()
    assert document["authorization_servers"] == ["https://auth.example.com"]
    assert document["resource"] == "https://api.example.com/mcp"


@pytest.mark.parametrize(("transport", "path", "post_to"), TRANSPORTS)
def test_both_transports_serve_the_same_document(transport, path, post_to):
    client = _app(transport, path, auth=_auth()).test_client()
    assert client.get(PROTECTED_RESOURCE_METADATA_PATH).json() == {
        "resource": "https://api.example.com/mcp",
        "authorization_servers": ["https://auth.example.com"],
        "bearer_methods_supported": ["header"],
    }


# ── no auth, no route ────────────────────────────────────────────────


@pytest.mark.parametrize(("transport", "path", "post_to"), TRANSPORTS)
def test_a_mount_without_auth_registers_no_metadata_route(transport, path, post_to):
    """There is no resource to describe, and nothing emits a challenge."""
    client = _app(transport, path).test_client()
    assert client.get(PROTECTED_RESOURCE_METADATA_PATH).status_code == 404


@pytest.mark.parametrize(("transport", "path", "post_to"), TRANSPORTS)
def test_a_mount_without_auth_does_not_refuse_a_request(transport, path, post_to):
    client = _app(transport, path).test_client()
    response = client.post(post_to, json=_ping(), headers={"Accept": "application/json"})
    assert response.status_code != 401


# ── two mounts register it once ──────────────────────────────────────


def test_mounting_both_transports_registers_the_route_once():
    """The path is fixed by the RFC, so a second registration would collide."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def probe() -> dict:
        return {}

    app.mount_mcp(transport="http", path="/mcp", auth=_auth())
    app.mount_mcp(transport="sse", path="/sse", auth=_auth())
    assert app.test_client().get(PROTECTED_RESOURCE_METADATA_PATH).status_code == 200


def test_both_endpoints_still_challenge_after_a_double_mount():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def probe() -> dict:
        return {}

    app.mount_mcp(transport="http", path="/mcp", auth=_auth())
    app.mount_mcp(transport="sse", path="/sse", auth=_auth())
    client = app.test_client()
    for post_to in ("/mcp", "/messages"):
        response = client.post(post_to, json=_ping(), headers={"Accept": "application/json"})
        assert response.status_code == 401


def test_an_sse_mount_after_an_unauthenticated_http_mount_still_registers_it():
    """The no-op guard must not skip a route the first mount never added."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def probe() -> dict:
        return {}

    app.mount_mcp(transport="http", path="/mcp")
    app.mount_mcp(transport="sse", path="/sse", auth=_auth())
    assert app.test_client().get(PROTECTED_RESOURCE_METADATA_PATH).status_code == 200


# ── authentication itself is unchanged ───────────────────────────────


@pytest.mark.parametrize(("transport", "path", "post_to"), TRANSPORTS)
def test_a_good_token_is_still_accepted(transport, path, post_to):
    client = _app(transport, path, auth=_auth()).test_client()
    response = client.post(
        post_to,
        json=_ping(),
        headers={"Accept": "application/json", "Authorization": "Bearer good"},
    )
    assert response.status_code != 401


@pytest.mark.parametrize(("transport", "path", "post_to"), TRANSPORTS)
def test_a_bad_token_is_still_refused(transport, path, post_to):
    client = _app(transport, path, auth=_auth()).test_client()
    response = client.post(
        post_to,
        json=_ping(),
        headers={"Accept": "application/json", "Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_the_metadata_route_needs_no_token():
    """It is what an unauthenticated client is sent to read."""
    client = _app("sse", "/sse", auth=_auth()).test_client()
    assert client.get(PROTECTED_RESOURCE_METADATA_PATH).status_code == 200


# ── the registration is shared, not copied ───────────────────────────


def test_the_transports_share_one_registration():
    """Two copies is how the two came to differ.

    Asserted against the parsed modules, so the argument names or a wrapped
    call signature do not decide whether this passes - only which module
    *defines* the function and which one *calls* it.
    """
    http = tree("transports", "http.py")
    sse = tree("transports", "sse.py")
    assert defines(http, "register_metadata_route")
    assert not defines(sse, "register_metadata_route")
    assert calls(sse, "register_metadata_route")
