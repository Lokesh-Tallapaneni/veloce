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
from veloce.security.jwt import (
    ExpiredSignatureError,
    InvalidAudienceError,
    InvalidSignatureError,
    decode_jwt,
    encode_jwt,
)

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
    subject: str | None = "user-1",
    resource: str | None = None,
) -> str:
    """Store a valid proxy-issued code and its upstream token set directly."""
    await proxy.store.save_upstream_tokens(
        jti,
        UpstreamTokenSet(
            access_token="upstream-access",
            refresh_token=refresh_token,
            expires_at=_now() + 3600,
            claims={"sub": subject},
            subject=subject,
            resource=resource,
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
    # The refresh token rotates unconditionally (C2); the JWT's own claims
    # can coincide with the original's when both mints land in the same
    # clock tick, so rotation is the assertion that can't be timing-flaky.
    assert body["refresh_token"] != refresh_token
    decode_jwt(body["access_token"], proxy.signing_key, algorithms=["HS256"])


async def test_refresh_with_an_unknown_or_expired_jti_is_rejected():
    proxy = _proxy()
    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": "never-issued"}
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_refreshing_with_the_jti_decoded_from_the_access_token_is_rejected():
    """A JWT's payload is signed, not encrypted - its `jti` claim is readable
    by anyone who ever sees the access token. It must not double as the
    refresh credential, or a leaked access token becomes permanent access."""
    proxy = _proxy()
    verifier, challenge = _pkce()
    code = await _seed(proxy, challenge, jti="jti-leak-check")

    async with AsyncTestClient(_app(proxy)) as client:
        issued = await client.post("/token", data=_token_form(code, verifier))
        claims = decode_jwt(issued.json()["access_token"], proxy.signing_key, algorithms=["HS256"])

        attempted = await client.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": claims["jti"]}
        )

    assert attempted.status_code == 400
    assert attempted.json()["error"] == "invalid_grant"


async def test_the_subject_is_preserved_when_a_refresh_response_omits_it(monkeypatch):
    """A refresh response typically carries no `id_token` at all, so the
    subject captured at the first exchange must survive, not be wiped."""
    httpx2 = pytest.importorskip("httpx2")
    proxy = _proxy()
    verifier, challenge = _pkce()
    code = await _seed(proxy, challenge, jti="jti-oidc", subject="real-upstream-user")

    def handler(request):
        return httpx2.Response(200, json={"access_token": "refreshed-access", "expires_in": 3600})

    transport = httpx2.MockTransport(handler)
    real_async_client = httpx2.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", patched_client)

    async with AsyncTestClient(_app(proxy)) as client:
        issued = await client.post("/token", data=_token_form(code, verifier))
        first_claims = decode_jwt(
            issued.json()["access_token"], proxy.signing_key, algorithms=["HS256"]
        )

        refreshed = await client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": issued.json()["refresh_token"]},
        )
        second_claims = decode_jwt(
            refreshed.json()["access_token"], proxy.signing_key, algorithms=["HS256"]
        )

    assert first_claims["sub"] == "real-upstream-user"
    assert second_claims["sub"] == "real-upstream-user"


async def test_a_malformed_upstream_refresh_response_is_rejected_cleanly(monkeypatch):
    """A 200 with no `access_token` must not crash the refresh grant."""
    httpx2 = pytest.importorskip("httpx2")
    proxy = _proxy()
    verifier, challenge = _pkce()
    code = await _seed(proxy, challenge, jti="jti-malformed")

    def handler(request):
        return httpx2.Response(200, text="not json")

    transport = httpx2.MockTransport(handler)
    real_async_client = httpx2.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", patched_client)

    async with AsyncTestClient(_app(proxy)) as client:
        issued = await client.post("/token", data=_token_form(code, verifier))
        response = await client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": issued.json()["refresh_token"]},
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_a_non_numeric_expires_in_on_refresh_is_rejected_cleanly(monkeypatch):
    """A 200 response that passes the access_token check but carries a
    non-numeric `expires_in` must not crash `float(...)` after the refresh
    token has already been read."""
    httpx2 = pytest.importorskip("httpx2")
    proxy = _proxy()
    verifier, challenge = _pkce()
    code = await _seed(proxy, challenge, jti="jti-bad-expiry")

    def handler(request):
        return httpx2.Response(200, json={"access_token": "refreshed-access", "expires_in": "soon"})

    transport = httpx2.MockTransport(handler)
    real_async_client = httpx2.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", patched_client)

    async with AsyncTestClient(_app(proxy)) as client:
        issued = await client.post("/token", data=_token_form(code, verifier))
        response = await client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": issued.json()["refresh_token"]},
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_a_transient_upstream_failure_during_refresh_does_not_burn_the_refresh_token(
    monkeypatch,
):
    """The refresh credential must survive a failed exchange attempt - a
    client retrying after a transient upstream hiccup should not be forced
    into a full /authorize re-consent because the first attempt consumed
    its only refresh token."""
    httpx2 = pytest.importorskip("httpx2")
    proxy = _proxy()
    verifier, challenge = _pkce()
    code = await _seed(proxy, challenge, jti="jti-transient")

    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx2.Response(503, text="upstream unavailable")
        return httpx2.Response(200, json={"access_token": "refreshed-access", "expires_in": 3600})

    transport = httpx2.MockTransport(handler)
    real_async_client = httpx2.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", patched_client)

    async with AsyncTestClient(_app(proxy)) as client:
        issued = await client.post("/token", data=_token_form(code, verifier))
        refresh_token = issued.json()["refresh_token"]

        failed = await client.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        # Retry with the SAME refresh token once the upstream recovers.
        retried = await client.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    assert failed.status_code == 400
    assert failed.json()["error"] == "invalid_grant"
    assert retried.status_code == 200
    decode_jwt(retried.json()["access_token"], proxy.signing_key, algorithms=["HS256"])


async def test_the_upstream_client_is_reused_across_token_exchanges(monkeypatch):
    """Opening a fresh connection (and TLS handshake) per call is wasted
    cost when every call targets the same upstream_token_endpoint - a
    persistent client should be created once and reused. Two refresh
    grants both call `_exchange_at_upstream`; a plain `authorization_code`
    grant never does (the exchange already happened at the callback)."""
    httpx2 = pytest.importorskip("httpx2")
    proxy = _proxy()
    verifier, challenge = _pkce()
    code = await _seed(proxy, challenge, jti="jti-reuse")

    created: list[object] = []
    real_async_client = httpx2.AsyncClient

    def handler(request):
        return httpx2.Response(200, json={"access_token": "upstream-access", "expires_in": 3600})

    transport = httpx2.MockTransport(handler)

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        instance = real_async_client(*args, **kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr(httpx2, "AsyncClient", patched_client)

    async with AsyncTestClient(_app(proxy)) as client:
        issued = await client.post("/token", data=_token_form(code, verifier))
        refresh_token = issued.json()["refresh_token"]
        first = await client.post(
            "/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        await client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": first.json()["refresh_token"]},
        )

    assert len(created) == 1


async def test_a_token_minted_for_one_resource_is_rejected_by_another():
    """RFC 8707 audience binding: a token requested for one MCP server must
    not be accepted by a verifier configured for a different one."""
    proxy = _proxy()
    verifier, challenge = _pkce()
    code = await _seed(proxy, challenge, resource="https://a.example/mcp")

    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.post("/token", data=_token_form(code, verifier))

    access_token = response.json()["access_token"]
    claims = decode_jwt(
        access_token,
        proxy.signing_key,
        algorithms=["HS256"],
        audience="https://a.example/mcp",
    )
    assert claims["aud"] == "https://a.example/mcp"
    with pytest.raises(InvalidAudienceError):
        decode_jwt(
            access_token,
            proxy.signing_key,
            algorithms=["HS256"],
            audience="https://b.example/mcp",
        )
