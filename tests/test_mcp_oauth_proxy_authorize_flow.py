"""The redirect half of the upstream OAuth proxy: `/authorize` and its callback.

`/authorize` never calls the upstream over the network - it only builds a
redirect - so only the callback's token exchange needs a mocked transport.
"""

from __future__ import annotations

import asyncio
import urllib.parse

import pytest

from veloce import AsyncTestClient, Veloce
from veloce.contrib.mcp.oauth_proxy import UpstreamOAuthProxy, _digest, _now

ISSUER = "https://proxy.example"
UPSTREAM_AUTHORIZE = "https://idp.example/authorize"
UPSTREAM_TOKEN = "https://idp.example/token"
CLIENT_REDIRECT_A = "http://127.0.0.1:9001/callback"
CLIENT_REDIRECT_B = "http://127.0.0.1:9002/callback"


def _proxy(**kwargs) -> UpstreamOAuthProxy:
    kwargs.setdefault("issuer", ISSUER)
    kwargs.setdefault("upstream_authorization_endpoint", UPSTREAM_AUTHORIZE)
    kwargs.setdefault("upstream_token_endpoint", UPSTREAM_TOKEN)
    kwargs.setdefault("upstream_client_id", "upstream-client")
    kwargs.setdefault("upstream_client_secret", "upstream-secret")
    kwargs.setdefault("signing_key", "signing-key")
    kwargs.setdefault("allowed_redirect_uris", [CLIENT_REDIRECT_A, CLIENT_REDIRECT_B])
    return UpstreamOAuthProxy(**kwargs)


def _app(proxy: UpstreamOAuthProxy) -> Veloce:
    from veloce.contrib.mcp.oauth_proxy import (
        _handle_authorize,
        _handle_register,
        _handle_token,
        _handle_upstream_callback,
    )

    app = Veloce(title="Proxy", version="1.0.0", openapi_url=None)

    async def authorize(request):
        return await _handle_authorize(proxy, request)

    async def callback(request):
        return await _handle_upstream_callback(proxy, request)

    async def token(request):
        return await _handle_token(proxy, request)

    async def register(request):
        return await _handle_register(proxy, request)

    app.add_route("/authorize", authorize, methods=["GET"], include_in_schema=False)
    app.add_route(proxy.redirect_path, callback, methods=["GET"], include_in_schema=False)
    app.add_route("/token", token, methods=["POST"], include_in_schema=False)
    app.add_route("/register", register, methods=["POST"], include_in_schema=False)
    return app


def _query(location: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(location)
    return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}


def _authorize_query(**overrides) -> str:
    params = {
        "redirect_uri": CLIENT_REDIRECT_A,
        "code_challenge": "client-challenge",
        "code_challenge_method": "S256",
        "state": "client-state",
    }
    params.update(overrides)
    return urllib.parse.urlencode(params)


def _mock_token_response(monkeypatch, handler) -> None:
    httpx2 = pytest.importorskip("httpx2")
    transport = httpx2.MockTransport(handler)
    real_async_client = httpx2.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", patched_client)


def _success_handler(httpx2):
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "access_token": "upstream-access",
                "refresh_token": "upstream-refresh",
                "expires_in": 3600,
            },
        )

    return handler


async def test_authorize_redirects_to_the_upstream_with_the_proxy_own_callback():
    proxy = _proxy()
    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.get(f"/authorize?{_authorize_query()}", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(UPSTREAM_AUTHORIZE)
    query = _query(location)
    assert query["redirect_uri"] == proxy.callback_url
    assert query["client_id"] == proxy.upstream_client_id
    assert query["response_type"] == "code"
    assert query["state"] != "client-state"


async def test_unregistered_redirect_uri_is_rejected_before_any_upstream_redirect():
    proxy = _proxy()
    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.get(
            f"/authorize?{_authorize_query(redirect_uri='http://attacker.example/callback')}",
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "location" not in response.headers


async def test_an_https_redirect_uri_outside_the_allowlist_is_rejected():
    """The scheme check alone is not enough: with no per-client registry, an
    attacker could otherwise send a victim a link whose redirect_uri is any
    https URL they control, harvest the resulting code, and redeem it
    themselves (the confused-deputy attack this allowlist closes)."""
    proxy = _proxy()
    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.get(
            f"/authorize?{_authorize_query(redirect_uri='https://attacker.example/callback')}",
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "location" not in response.headers


async def test_an_empty_allowlist_rejects_every_redirect_uri():
    """Fail closed: a proxy built without `allowed_redirect_uris` accepts
    nothing, rather than falling back to "any https URL is fine"."""
    proxy = _proxy(allowed_redirect_uris=())
    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.get(f"/authorize?{_authorize_query()}", follow_redirects=False)

    assert response.status_code == 400
    assert "location" not in response.headers


async def test_register_rejects_a_redirect_uri_outside_the_allowlist():
    proxy = _proxy()
    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.post(
            "/register", json={"redirect_uris": ["https://attacker.example/callback"]}
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


async def test_register_accepts_a_redirect_uri_on_the_allowlist():
    proxy = _proxy()
    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.post("/register", json={"redirect_uris": [CLIENT_REDIRECT_A]})

    assert response.status_code == 201


async def test_a_stale_pending_authorization_is_rejected_at_the_callback():
    """Review Focus #4 edge case: an abandoned flow's pending record expires,
    rather than staying redeemable - and callable - forever."""
    proxy = _proxy()
    async with AsyncTestClient(_app(proxy)) as client:
        authorize_response = await client.get(
            f"/authorize?{_authorize_query()}", follow_redirects=False
        )
        upstream_state = _query(authorize_response.headers["location"])["state"]

        key = _digest(upstream_state)
        pending = await proxy.store.take_pending(key)
        assert pending is not None
        pending.expires_at = _now() - 1
        await proxy.store.save_pending(key, pending)

        response = await client.get(
            f"{proxy.redirect_path}?code=upstream-code&state={upstream_state}",
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "location" not in response.headers


async def test_upstream_callback_completes_the_flow_and_redirects_to_the_client(monkeypatch):
    httpx2 = pytest.importorskip("httpx2")
    _mock_token_response(monkeypatch, _success_handler(httpx2))
    proxy = _proxy()

    async with AsyncTestClient(_app(proxy)) as client:
        authorize_response = await client.get(
            f"/authorize?{_authorize_query()}", follow_redirects=False
        )
        upstream_state = _query(authorize_response.headers["location"])["state"]

        callback_response = await client.get(
            f"{proxy.redirect_path}?code=upstream-code&state={upstream_state}",
            follow_redirects=False,
        )

    assert callback_response.status_code == 302
    location = callback_response.headers["location"]
    assert location.startswith(CLIENT_REDIRECT_A)
    query = _query(location)
    assert query["state"] == "client-state"
    assert query["code"]


async def test_callback_with_an_unknown_state_is_rejected():
    proxy = _proxy()
    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.get(
            f"{proxy.redirect_path}?code=upstream-code&state=never-issued",
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "location" not in response.headers


async def test_upstream_token_exchange_failure_surfaces_cleanly_and_leaves_no_orphaned_state(
    monkeypatch,
):
    httpx2 = pytest.importorskip("httpx2")

    def failing_handler(request):
        return httpx2.Response(400, json={"error": "invalid_grant"})

    _mock_token_response(monkeypatch, failing_handler)
    proxy = _proxy()

    async with AsyncTestClient(_app(proxy)) as client:
        authorize_response = await client.get(
            f"/authorize?{_authorize_query()}", follow_redirects=False
        )
        upstream_state = _query(authorize_response.headers["location"])["state"]

        first = await client.get(
            f"{proxy.redirect_path}?code=upstream-code&state={upstream_state}",
            follow_redirects=False,
        )
        # Replaying the same state proves the pending record was consumed, not
        # left stored, by the failed attempt above.
        second = await client.get(
            f"{proxy.redirect_path}?code=upstream-code&state={upstream_state}",
            follow_redirects=False,
        )

    assert first.status_code == 302
    assert first.headers["location"].startswith(CLIENT_REDIRECT_A)
    assert _query(first.headers["location"])["error"]
    assert second.status_code == 400
    assert "location" not in second.headers


async def test_two_concurrent_authorize_flows_do_not_cross_contaminate(monkeypatch):
    httpx2 = pytest.importorskip("httpx2")
    _mock_token_response(monkeypatch, _success_handler(httpx2))
    proxy = _proxy()

    async with AsyncTestClient(_app(proxy)) as client:
        response_a, response_b = await asyncio.gather(
            client.get(
                f"/authorize?{_authorize_query(redirect_uri=CLIENT_REDIRECT_A, state='state-a')}",
                follow_redirects=False,
            ),
            client.get(
                f"/authorize?{_authorize_query(redirect_uri=CLIENT_REDIRECT_B, state='state-b')}",
                follow_redirects=False,
            ),
        )
        state_a = _query(response_a.headers["location"])["state"]
        state_b = _query(response_b.headers["location"])["state"]

        callback_a, callback_b = await asyncio.gather(
            client.get(
                f"{proxy.redirect_path}?code=code-a&state={state_a}", follow_redirects=False
            ),
            client.get(
                f"{proxy.redirect_path}?code=code-b&state={state_b}", follow_redirects=False
            ),
        )

    assert callback_a.headers["location"].startswith(CLIENT_REDIRECT_A)
    assert _query(callback_a.headers["location"])["state"] == "state-a"
    assert callback_b.headers["location"].startswith(CLIENT_REDIRECT_B)
    assert _query(callback_b.headers["location"])["state"] == "state-b"

    code_a = _query(callback_a.headers["location"])["code"]
    code_b = _query(callback_b.headers["location"])["code"]
    key_a = (await proxy.store.take_code(_digest(code_a))).jti
    key_b = (await proxy.store.take_code(_digest(code_b))).jti
    assert key_a != key_b


async def test_authorize_sends_upstream_scopes_not_scopes_supported_to_the_upstream():
    """`scopes_supported` is what the proxy grants to MCP clients; it is not
    vocabulary the upstream IdP recognizes. Sending it as `scope` at
    `/authorize` would make a real provider refuse with `invalid_scope`."""
    proxy = _proxy(scopes_supported=["mcp:tools"], upstream_scopes=["openid", "email"])
    async with AsyncTestClient(_app(proxy)) as client:
        response = await client.get(f"/authorize?{_authorize_query()}", follow_redirects=False)

    scope = _query(response.headers["location"])["scope"]
    assert scope == "openid email"
    assert "mcp:tools" not in scope


async def test_a_malformed_upstream_response_surfaces_cleanly_instead_of_crashing(monkeypatch):
    """A 200 response with no `access_token` must not crash the callback -
    the pending record is already consumed by this point, so a crash would
    give the client a server error page instead of a clean redirect."""
    httpx2 = pytest.importorskip("httpx2")

    def handler(request):
        return httpx2.Response(200, json={"not_a_token_response": True})

    _mock_token_response(monkeypatch, handler)
    proxy = _proxy()

    async with AsyncTestClient(_app(proxy)) as client:
        authorize_response = await client.get(
            f"/authorize?{_authorize_query()}", follow_redirects=False
        )
        upstream_state = _query(authorize_response.headers["location"])["state"]

        response = await client.get(
            f"{proxy.redirect_path}?code=upstream-code&state={upstream_state}",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"].startswith(CLIENT_REDIRECT_A)
    assert _query(response.headers["location"])["error"]
