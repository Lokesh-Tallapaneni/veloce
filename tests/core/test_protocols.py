"""Pluggable-backend protocols are satisfied structurally, by shipped and user classes alike."""

from __future__ import annotations

import veloce
from veloce import (
    Cache,
    CacheBackend,
    DefaultJSONProvider,
    InMemoryCache,
    InMemoryRateLimitBackend,
    InMemorySessionStore,
    JSONProviderBackend,
    RateLimitStoreBackend,
    SessionStoreBackend,
    Veloce,
    protocols,
)


def test_shipped_backends_satisfy_their_protocols():
    assert isinstance(InMemoryCache(), CacheBackend)
    assert isinstance(InMemorySessionStore(), SessionStoreBackend)
    assert isinstance(InMemoryRateLimitBackend(), RateLimitStoreBackend)
    assert isinstance(DefaultJSONProvider(Veloce()), JSONProviderBackend)


def test_a_class_that_does_not_inherit_still_satisfies_a_protocol():
    class Duck:
        async def get(self, key):
            return None

        async def set(self, key, value, ttl):
            return None

        async def delete(self, key):
            return None

    assert isinstance(Duck(), CacheBackend)
    assert not isinstance(Duck(), SessionStoreBackend)
    assert not issubclass(Duck, Cache)


def test_an_object_missing_a_method_does_not_satisfy_the_protocol():
    class Half:
        async def get(self, key):
            return None

    assert not isinstance(Half(), CacheBackend)


def test_protocols_module_reexports_existing_protocols_lazily():
    for name in ("Plugin", "AuthorizationStore", "Transport", "SessionBackend"):
        assert getattr(protocols, name) is not None
    assert "Plugin" in dir(protocols)


def test_protocols_are_public_names():
    for name in (
        "CacheBackend",
        "SessionStoreBackend",
        "RateLimitStoreBackend",
        "JSONProviderBackend",
    ):
        assert name in veloce.__all__
