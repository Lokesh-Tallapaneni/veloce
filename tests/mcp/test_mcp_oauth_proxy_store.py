"""Tests for the OAuth-proxy data model, store, and OIDC discovery helper."""

from __future__ import annotations

import sys

import pytest

from veloce.contrib.mcp.oauth_proxy import (
    InMemoryUpstreamOAuthProxyStore,
    PendingAuthorization,
    UpstreamTokenSet,
    discover_oidc_endpoints,
)


async def test_pending_authorization_round_trips_through_the_store():
    """Save then take returns the same values; take again returns None
    (one-shot, matching authorization.py's existing take_code semantics)."""
    store = InMemoryUpstreamOAuthProxyStore()
    pending = PendingAuthorization(
        client_redirect_uri="https://client.example/callback",
        client_code_challenge="challenge",
        client_state="xyz",
    )

    await store.save_pending("key", pending)

    assert await store.take_pending("key") == pending
    assert await store.take_pending("key") is None


async def test_upstream_tokens_round_trip_and_delete():
    """save/get returns the same UpstreamTokenSet; delete then get returns None."""
    store = InMemoryUpstreamOAuthProxyStore()
    tokens = UpstreamTokenSet(
        access_token="upstream-access",
        refresh_token="upstream-refresh",
        expires_at=1234.0,
        claims={"sub": "u1"},
    )

    await store.save_upstream_tokens("key", tokens)

    assert await store.get_upstream_tokens("key") == tokens
    await store.delete_upstream_tokens("key")
    assert await store.get_upstream_tokens("key") is None


async def test_discover_oidc_endpoints_parses_the_well_known_document(monkeypatch):
    """Mock the issuer's /.well-known/openid-configuration response
    (httpx2.MockTransport, same pattern test_mcp_http_upstream.py already
    uses) and assert the two endpoint URLs are extracted correctly."""
    httpx2 = pytest.importorskip("httpx2")

    document = {
        "authorization_endpoint": "https://idp.example/authorize",
        "token_endpoint": "https://idp.example/token",
    }

    def handler(request):
        assert request.url.path == "/.well-known/openid-configuration"
        return httpx2.Response(200, json=document)

    transport = httpx2.MockTransport(handler)
    real_async_client = httpx2.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", patched_client)

    endpoints = await discover_oidc_endpoints("https://idp.example")

    assert endpoints == (document["authorization_endpoint"], document["token_endpoint"])


async def test_discover_oidc_endpoints_on_a_pre_311_interpreter_names_the_python_floor(
    monkeypatch,
):
    """Same sys.modules[name]=None trick http_upstream.py's own tests use."""
    monkeypatch.setitem(sys.modules, "httpx2", None)
    monkeypatch.setattr(sys, "version_info", (3, 10, 0, "final", 0))

    with pytest.raises(RuntimeError, match="Python >= 3.11"):
        await discover_oidc_endpoints("https://idp.example")
