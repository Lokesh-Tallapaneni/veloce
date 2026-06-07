"""Redis-backed session store and rate limiter (veloce.contrib.redis)."""

from __future__ import annotations

import pytest

fakeredis = pytest.importorskip("fakeredis")

from fakeredis.aioredis import FakeRedis  # noqa: E402

from veloce import (  # noqa: E402
    FixedWindow,
    RateLimitMiddleware,
    Request,
    TokenBucket,
    Veloce,
)
from veloce.contrib.redis import RedisRateLimitBackend, RedisSessionStore  # noqa: E402
from veloce.testclient import TestClient  # noqa: E402


@pytest.fixture
def client() -> FakeRedis:
    return FakeRedis()


# ── RedisSessionStore ────────────────────────────────────────────────


async def test_session_write_read_round_trip(client):
    store = RedisSessionStore(client)
    await store.write("sid1", {"user": 7, "roles": ["a"]}, 60)
    assert await store.read("sid1") == {"user": 7, "roles": ["a"]}


async def test_session_read_missing_returns_none(client):
    store = RedisSessionStore(client)
    assert await store.read("nope") is None


async def test_session_delete_revokes(client):
    store = RedisSessionStore(client)
    await store.write("sid", {"x": 1}, 60)
    await store.delete("sid")
    assert await store.read("sid") is None


async def test_session_replace_only_when_present(client):
    store = RedisSessionStore(client)
    # Absent id: conditional write fails and does not create the key.
    assert await store.replace("ghost", {"x": 1}, 60) is False
    assert await store.read("ghost") is None
    # Present id: conditional write succeeds.
    await store.write("sid", {"x": 1}, 60)
    assert await store.replace("sid", {"x": 2}, 60) is True
    assert await store.read("sid") == {"x": 2}


async def test_session_touch_refreshes_only_existing(client):
    store = RedisSessionStore(client)
    assert await store.touch("ghost", 60) is False
    await store.write("sid", {"x": 1}, 1)
    assert await store.touch("sid", 120) is True
    # TTL was extended without moving the payload.
    assert await store.read("sid") == {"x": 1}
    assert await client.ttl("veloce:session:sid") > 1


async def test_session_expiry_sets_redis_ttl(client):
    store = RedisSessionStore(client)
    await store.write("sid", {"x": 1}, 45)
    assert 0 < await client.ttl("veloce:session:sid") <= 45


async def test_session_custom_prefix(client):
    store = RedisSessionStore(client, prefix="app:s:")
    await store.write("sid", {"x": 1}, 60)
    assert await client.get("app:s:sid") is not None


# ── RedisRateLimitBackend ────────────────────────────────────────────

# A stable User-Agent so the per-client bucket key is deterministic under
# TestClient (which carries no transport peer address).
_UA = {"User-Agent": "rl-test"}


def _app_with_limiter(client, strategy) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(
        RateLimitMiddleware(strategy=strategy, backend=RedisRateLimitBackend(client))
    )

    @app.get("/")
    async def index(request: Request):
        return {"ok": True}

    return app


def test_rate_limiter_allows_under_limit(client):
    app = _app_with_limiter(client, FixedWindow(3, 60))
    with TestClient(app) as tc:
        for _ in range(3):
            assert tc.get("/", headers=_UA).status_code == 200


def test_rate_limiter_rejects_over_limit(client):
    app = _app_with_limiter(client, FixedWindow(2, 60))
    with TestClient(app) as tc:
        assert tc.get("/", headers=_UA).status_code == 200
        assert tc.get("/", headers=_UA).status_code == 200
        resp = tc.get("/", headers=_UA)
        assert resp.status_code == 429
        assert resp.headers["Retry-After"]
        assert resp.headers["X-RateLimit-Remaining"] == "0"


def test_rate_limiter_shares_state_across_instances(client):
    # Two limiter instances backed by the same Redis enforce one shared count -
    # the cross-worker property the in-process backend lacks.
    app1 = _app_with_limiter(client, FixedWindow(2, 60))
    app2 = _app_with_limiter(client, FixedWindow(2, 60))
    with TestClient(app1) as tc1, TestClient(app2) as tc2:
        assert tc1.get("/", headers=_UA).status_code == 200
        assert tc2.get("/", headers=_UA).status_code == 200
        # The shared counter is now at the limit regardless of which app serves.
        assert tc1.get("/", headers=_UA).status_code == 429


def test_token_bucket_over_redis(client):
    # A drained bucket (burst=2) rejects the third immediate request, and the
    # decision round-trips through the WATCH/MULTI transaction on Redis.
    app = _app_with_limiter(client, TokenBucket(rate=2, per=60, burst=2))
    with TestClient(app) as tc:
        assert tc.get("/", headers=_UA).status_code == 200
        assert tc.get("/", headers=_UA).status_code == 200
        assert tc.get("/", headers=_UA).status_code == 429


async def test_backend_retries_on_watch_conflict(client):
    # Force the first EXEC to abort with a WatchError by mutating the watched
    # key mid-transaction; the backend must re-read and retry, not error.
    backend = RedisRateLimitBackend(client)
    real_pipeline = client.pipeline
    state = {"first": True}

    class RetryPipe:
        def __init__(self, inner):
            self._inner = inner

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._inner.__aexit__(*exc)

        async def watch(self, *a):
            return await self._inner.watch(*a)

        async def get(self, *a):
            return await self._inner.get(*a)

        def multi(self):
            return self._inner.multi()

        def set(self, *a, **k):
            return self._inner.set(*a, **k)

        async def execute(self):
            if state["first"]:
                state["first"] = False
                await client.set("veloce:ratelimit:c", b'{"window": 0, "count": 1}')
            return await self._inner.execute()

    client.pipeline = lambda *a, **k: RetryPipe(real_pipeline(*a, **k))
    result = await backend.evaluate("c", FixedWindow(5, 60), 0.0)
    assert result.allowed
    assert state["first"] is False  # the retry branch ran


async def test_backend_falls_back_after_exhausting_retries(client):
    # Every EXEC aborts with a WatchError; after the retry cap the backend must
    # serve the request via the non-atomic fallback rather than livelock.
    backend = RedisRateLimitBackend(client)
    real_pipeline = client.pipeline

    class AlwaysConflictPipe:
        def __init__(self, inner):
            self._inner = inner

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._inner.__aexit__(*exc)

        async def watch(self, *a):
            return await self._inner.watch(*a)

        async def get(self, *a):
            return await self._inner.get(*a)

        def multi(self):
            return self._inner.multi()

        def set(self, *a, **k):
            return self._inner.set(*a, **k)

        async def execute(self):
            await client.set("veloce:ratelimit:hot", b'{"window": 0, "count": 1}')
            return await self._inner.execute()

    client.pipeline = lambda *a, **k: AlwaysConflictPipe(real_pipeline(*a, **k))
    result = await backend.evaluate("hot", FixedWindow(5, 60), 0.0)
    assert result.allowed
    # The fallback wrote state through the plain (non-pipeline) client.
    assert await client.get("veloce:ratelimit:hot") is not None


def test_backend_from_url_builds_client(client, monkeypatch):
    # from_url defers to the shared loader; patch it so no real connection is made.
    import veloce.contrib.redis as redis_mod

    monkeypatch.setattr(redis_mod, "_load_redis_from_url", lambda url, **kw: client)
    backend = RedisRateLimitBackend.from_url("redis://localhost:6379/0", prefix="rl:")
    assert backend._prefix == "rl:"


# ── RedisCache ────────────────────────────────────────────────────────


async def test_redis_cache_round_trip(client):
    from veloce.contrib.redis import RedisCache

    cache = RedisCache(client)
    await cache.set("k", b"value", 60)
    assert await cache.get("k") == b"value"
    assert await client.ttl("veloce:cache:k") > 0
    await cache.delete("k")
    assert await cache.get("k") is None


async def test_redis_cache_missing_returns_none(client):
    from veloce.contrib.redis import RedisCache

    assert await RedisCache(client).get("absent") is None


async def test_cached_decorator_with_redis_backend(client):
    from veloce.cache import cached
    from veloce.contrib.redis import RedisCache

    cache = RedisCache(client)
    calls = 0

    @cached(cache, ttl=60)
    async def compute(n: int) -> dict:
        nonlocal calls
        calls += 1
        return {"n": n}

    assert await compute(3) == {"n": 3}
    assert await compute(3) == {"n": 3}
    assert calls == 1
