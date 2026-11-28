"""A verifier configured with `resource=` refuses a token that names no resource.

`verifier(resource=...)` exists to stop, in its own words, "a token obtained for
one MCP server being replayed against another sharing the authorization server"
(RFC 8707 audience binding). The check was:

    if resource is not None and record.resource is not None and record.resource != resource

`resource` is optional at `/authorize`, so a client that simply omits it gets a
token with `record.resource = None`, the middle clause is false, and the token is
accepted by every resource server sharing that authorization server - which is
the replay the parameter was added to prevent, reachable by asking for less.

An unbound token cannot satisfy an audience requirement. When the operator has
configured `resource=`, a token that names no audience is refused; a verifier
built without `resource=` is unchanged (it already warns that binding is not
enforced).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse

import pytest

from veloce import AsyncTestClient, Principal, Veloce
from veloce.contrib.mcp import (
    InMemoryAuthorizationStore,
    MCPAuthorizationServer,
    register_authorization_server,
)

ISSUER = "https://api.example.com"
RESOURCE = f"{ISSUER}/mcp"
OTHER_RESOURCE = f"{ISSUER}/other"
REDIRECT = "http://127.0.0.1:9876/callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def _server() -> MCPAuthorizationServer:
    return MCPAuthorizationServer(
        issuer=ISSUER,
        authenticate=lambda request: Principal(subject="user-42", scopes={"mcp:tools"}),
        scopes_supported=["mcp:tools"],
        store=InMemoryAuthorizationStore(),
    )


def _app(server: MCPAuthorizationServer) -> Veloce:
    app = Veloce(title="Secured", version="1.0.0", openapi_url=None)
    register_authorization_server(app, server)
    return app


async def _token_for(client, resource: str | None) -> str:
    """Run the full code flow, requesting `resource` only when one is given."""
    registered = json.loads(
        (await client.post("/register", json={"redirect_uris": [REDIRECT]})).body
    )
    client_id = registered["client_id"]
    verifier, challenge = _pkce()

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        "scope": "mcp:tools",
    }
    if resource is not None:
        params["resource"] = resource

    redirected = await client.get(
        f"/authorize?{urllib.parse.urlencode(params)}", follow_redirects=False
    )
    code = urllib.parse.parse_qs(urllib.parse.urlparse(redirected.headers["location"]).query)[
        "code"
    ][0]

    issued = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    return json.loads(issued.body)["access_token"]


async def test_a_token_bound_to_this_resource_is_accepted():
    """The control: audience binding must still admit the right token."""
    server = _server()
    async with AsyncTestClient(_app(server)) as client:
        token = await _token_for(client, RESOURCE)

    principal = await server.verifier(resource=RESOURCE)(token)

    assert principal is not None
    assert principal.claims["aud"] == RESOURCE


async def test_a_token_bound_to_another_resource_is_refused():
    """The half that already worked."""
    server = _server()
    async with AsyncTestClient(_app(server)) as client:
        token = await _token_for(client, OTHER_RESOURCE)

    assert await server.verifier(resource=RESOURCE)(token) is None


async def test_a_token_bound_to_nothing_is_refused():
    """The regression: omitting `resource` at /authorize bypassed the binding."""
    server = _server()
    async with AsyncTestClient(_app(server)) as client:
        token = await _token_for(client, None)

    assert await server.verifier(resource=RESOURCE)(token) is None, (
        "an unbound token is accepted by every server sharing this authorization server"
    )


async def test_an_unconfigured_verifier_still_accepts_an_unbound_token():
    """A verifier built without `resource=` enforces no binding, and says so."""
    server = _server()
    async with AsyncTestClient(_app(server)) as client:
        token = await _token_for(client, None)

    with pytest.warns(UserWarning, match="audience binding"):
        verify = server.verifier()

    assert await verify(token) is not None


async def test_an_unconfigured_verifier_still_accepts_a_bound_token():
    """The unconfigured verifier must not start filtering either."""
    server = _server()
    async with AsyncTestClient(_app(server)) as client:
        token = await _token_for(client, RESOURCE)

    with pytest.warns(UserWarning, match="audience binding"):
        verify = server.verifier()

    assert await verify(token) is not None


@pytest.mark.parametrize("requested", [None, OTHER_RESOURCE], ids=["unbound", "other"])
async def test_no_token_but_the_right_one_gets_through(requested):
    """Stated together: exactly one of the three shapes is admissible."""
    server = _server()
    async with AsyncTestClient(_app(server)) as client:
        token = await _token_for(client, requested)

    assert await server.verifier(resource=RESOURCE)(token) is None
