"""The full OAuth-proxy flow, through a real app and a real test client.

Every other test in this feature drives one leg in isolation; this one
proves the legs actually connect: DCR -> /authorize -> a mocked upstream
callback -> /token -> a minted JWT -> that JWT authorizing a real MCP call.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse

import pytest

from veloce import AsyncTestClient, Principal, Veloce
from veloce.contrib.mcp import MCPAuth, UpstreamOAuthProxy, register_oauth_proxy
from veloce.security.jwt import JWTError, decode_jwt

ISSUER = "https://proxy.example"
UPSTREAM_AUTHORIZE = "https://idp.example/authorize"
UPSTREAM_TOKEN = "https://idp.example/token"
RESOURCE = f"{ISSUER}/mcp"
CLIENT_REDIRECT = "http://127.0.0.1:9001/callback"


def _proxy() -> UpstreamOAuthProxy:
    return UpstreamOAuthProxy(
        issuer=ISSUER,
        upstream_authorization_endpoint=UPSTREAM_AUTHORIZE,
        upstream_token_endpoint=UPSTREAM_TOKEN,
        upstream_client_id="upstream-client",
        upstream_client_secret="upstream-secret",
        signing_key="signing-key",
        scopes_supported=["mcp:tools"],
        allowed_redirect_uris=[CLIENT_REDIRECT],
    )


def _verifier(proxy: UpstreamOAuthProxy):
    async def verify(token: str) -> Principal | None:
        try:
            claims = decode_jwt(token, proxy.signing_key, algorithms=["HS256"], issuer=proxy.issuer)
        except JWTError:
            return None
        return Principal(
            subject=claims.get("sub"),
            scopes=frozenset((claims.get("scope") or "").split()),
        )

    return verify


def _app(proxy: UpstreamOAuthProxy) -> Veloce:
    app = Veloce(title="Full", version="1.0.0", openapi_url=None)
    register_oauth_proxy(app, proxy)

    @app.mcp_tool(description="Name the caller", scopes=["mcp:tools"])
    async def whoami() -> str:
        return "ok"

    app.mount_mcp(
        transport="http",
        auth=MCPAuth(
            verify=_verifier(proxy),
            resource_server_url=RESOURCE,
            authorization_servers=[proxy.issuer],
        ),
    )
    return app


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def _query(location: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(location)
    return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}


def _tamper_signature(jwt: str) -> str:
    """Flip the signature's first base64url character, guaranteed to change
    the decoded bytes - unlike the last character, which can fall in a
    partial encoding group's unused bits and decode unchanged."""
    header, payload, signature = jwt.rsplit(".", 2)
    replacement = "A" if signature[0] != "A" else "B"
    return f"{header}.{payload}.{replacement}{signature[1:]}"


def _mock_upstream_token_endpoint(monkeypatch) -> None:
    httpx2 = pytest.importorskip("httpx2")

    def handler(request):
        return httpx2.Response(
            200,
            json={
                "access_token": "upstream-access",
                "refresh_token": "upstream-refresh",
                "expires_in": 3600,
            },
        )

    transport = httpx2.MockTransport(handler)
    real_async_client = httpx2.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", patched_client)


async def _mint_access_token_through_the_full_flow(client, proxy: UpstreamOAuthProxy) -> str:
    verifier, challenge = _pkce()

    registered = await client.post("/register", json={"redirect_uris": [CLIENT_REDIRECT]})
    assert registered.status_code == 201

    authorize = await client.get(
        "/authorize"
        f"?redirect_uri={urllib.parse.quote(CLIENT_REDIRECT)}"
        f"&code_challenge={challenge}&code_challenge_method=S256&state=client-state",
        follow_redirects=False,
    )
    upstream_state = _query(authorize.headers["location"])["state"]

    callback = await client.get(
        f"{proxy.redirect_path}?code=upstream-code&state={upstream_state}",
        follow_redirects=False,
    )
    callback_query = _query(callback.headers["location"])
    assert callback.headers["location"].startswith(CLIENT_REDIRECT)
    assert callback_query["state"] == "client-state"

    token_response = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": callback_query["code"],
            "code_verifier": verifier,
            "redirect_uri": CLIENT_REDIRECT,
        },
    )
    assert token_response.status_code == 200
    access_token: str = token_response.json()["access_token"]
    return access_token


def _whoami_call() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "whoami", "arguments": {}},
    }


async def test_the_full_oauth_proxy_flow_end_to_end_through_a_real_app(monkeypatch):
    _mock_upstream_token_endpoint(monkeypatch)
    proxy = _proxy()

    async with AsyncTestClient(_app(proxy)) as client:
        access_token = await _mint_access_token_through_the_full_flow(client, proxy)

        call = await client.post(
            "/mcp",
            json=_whoami_call(),
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert call.status_code == 200
    body = call.json()
    assert "error" not in body
    assert body["result"]["content"][0]["text"] == "ok"


async def test_a_tampered_proxy_minted_jwt_is_rejected_at_a_real_tool_call(monkeypatch):
    """Review Focus #6, end to end: a proxy-minted JWT with a flipped
    signature byte must fail at the resource server, not just at a bare
    `decode_jwt` call divorced from the actual verification path."""
    _mock_upstream_token_endpoint(monkeypatch)
    proxy = _proxy()

    async with AsyncTestClient(_app(proxy)) as client:
        access_token = await _mint_access_token_through_the_full_flow(client, proxy)
        tampered = _tamper_signature(access_token)

        call = await client.post(
            "/mcp",
            json=_whoami_call(),
            headers={"Authorization": f"Bearer {tampered}"},
        )

    assert call.status_code == 401
