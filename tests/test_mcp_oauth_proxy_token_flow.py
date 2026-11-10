"""The client-facing half of the upstream OAuth proxy: `/token`.

Picks up where a proxy-issued authorization code already exists - the
store is seeded directly rather than re-driving the full redirect dance
`test_mcp_oauth_proxy_authorize_flow.py` already covers.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

import pytest

from veloce import AsyncTestClient, Veloce
from veloce.contrib.mcp.oauth_proxy import (
    ProxyAuthorizationCode,
    UpstreamOAuthProxy,
    UpstreamTokenSet,
    _digest,
    _handle_token,
    _now,
)
from veloce.security.jwt import ExpiredSignatureError, InvalidSignatureError, decode_jwt, encode_jwt

ISSUER = "https://proxy.example"
UPSTREAM_TOKEN = "https://idp.example/token"
CLIENT_REDIRECT = "http://127.0.0.1:9001/callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def _proxy(**kwargs) -> UpstreamOAuthProxy:
    kwargs.setdefault("issuer", ISSUER)
    kwargs.setdefault("upstream_authorization_endpoint", "https://idp.example/authorize")
    kwargs.setdefault("upstream_token_endpoint", UPSTREAM_TOKEN)
    kwargs.setdefault("upstream_client_id", "upstream-client")
    kwargs.setdefault("upstream_client_secret", "upstream-secret")
    kwargs.setdefault("signing_key", "signing-key")
    kwargs.setdefault("scopes_supported", ["mcp:tools"])
    return UpstreamOAuthProxy(**kwargs)


def _app(proxy: UpstreamOAuthProxy) -> Veloce:
    app = Veloce(title="Proxy", version="1.0.0", openapi_url=None)

    async def token(request):
        return await _handle_token(proxy, request)

    app.add_route("/token", token, methods=["POST"], include_in_schema=False)
    return app


async def _seed(
    proxy: UpstreamOAuthProxy,
    challenge: str,
    *,
    jti: str = "jti-1",
    refresh_token: str | None = "upstream-refresh",
) -> str:
    """Store a valid proxy-issued code and its upstream token set directly."""
    await proxy.store.save_upstream_tokens(
        jti,
        UpstreamTokenSet(
            access_token="upstream-access",
            refresh_token=refresh_token,
            expires_at=_now() + 3600,
            claims={"sub": "user-1"},
        ),
    )
    code = secrets.token_urlsafe(24)
    await proxy.store.save_code(
        _digest(code),
        ProxyAuthorizationCode(
            client_redirect_uri=CLIENT_REDIRECT,
            client_code_challenge=challenge,
            jti=jti,
            expires_at=_now() + 60,
        ),
    )
    return code


def _token_form(code: str, verifier: str, **overrides) -> dict:
    params = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": CLIENT_REDIRECT,
    }
    params.update(overrides)
    return params


async def test_token_exchange_with_the_right_pkce_verifier_mints_a_valid_jwt():
    proxy = _proxy()
    verifier, challenge = _pkce()
    code = await _seed(proxy, challenge)

    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.post("/token", data=_token_form(code, verifier))

    assert response.status_code == 200
    body = response.json()
    claims = decode_jwt(
        body["access_token"], proxy.signing_key, algorithms=["HS256"], issuer=proxy.issuer
    )
    assert claims["iss"] == proxy.issuer
    assert claims["scope"] == "mcp:tools"


async def test_token_exchange_with_the_wrong_pkce_verifier_is_rejected():
    proxy = _proxy()
    _verifier, challenge = _pkce()
    code = await _seed(proxy, challenge)

    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.post("/token", data=_token_form(code, "wrong-verifier"))

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_replaying_an_already_used_code_is_rejected():
    proxy = _proxy()
    verifier, challenge = _pkce()
    code = await _seed(proxy, challenge)

    async with AsyncTestClient(_app(proxy)) as client:
        first = await client.post("/token", data=_token_form(code, verifier))
        second = await client.post("/token", data=_token_form(code, verifier))

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"


async def test_a_tampered_jwt_is_rejected_by_mcpauth_verification():
    jwt = encode_jwt({"iss": ISSUER, "sub": "user-1"}, "signing-key", alg="HS256")
    header, payload, signature = jwt.rsplit(".", 2)
    tampered = f"{header}.{payload}.{signature[:-1]}{'A' if signature[-1] != 'A' else 'B'}"

    with pytest.raises(InvalidSignatureError):
        decode_jwt(tampered, "signing-key", algorithms=["HS256"])


async def test_an_expired_jwt_is_rejected():
    proxy = _proxy(access_token_ttl=0.0)
    verifier, challenge = _pkce()
    code = await _seed(proxy, challenge)

    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.post("/token", data=_token_form(code, verifier))
    jwt = response.json()["access_token"]

    with pytest.raises(ExpiredSignatureError):
        decode_jwt(jwt, proxy.signing_key, algorithms=["HS256"])


async def test_refresh_with_a_valid_refresh_token_remints_a_fresh_jwt(monkeypatch):
    httpx2 = pytest.importorskip("httpx2")
    proxy = _proxy()
    verifier, challenge = _pkce()
    code = await _seed(proxy, challenge, jti="jti-refresh")

    def handler(request):
        return httpx2.Response(
            200,
            json={"access_token": "new-upstream-access", "expires_in": 3600},
        )

    transport = httpx2.MockTransport(handler)
    real_async_client = httpx2.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", patched_client)

    async with AsyncTestClient(_app(proxy)) as client:
        issued = await client.post("/token", data=_token_form(code, verifier))
        refresh_token = issued.json()["refresh_token"]

        refreshed = await client.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["access_token"] != issued.json()["access_token"]
    decode_jwt(body["access_token"], proxy.signing_key, algorithms=["HS256"])


async def test_refresh_with_an_unknown_or_expired_jti_is_rejected():
    proxy = _proxy()
    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": "never-issued"}
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
