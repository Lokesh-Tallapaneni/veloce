"""Redis-backed session store and rate limiter (veloce.contrib.redis)."""

from __future__ import annotations

import pytest

fakeredis = pytest.importorskip("fakeredis")

from fakeredis.aioredis import FakeRedis  # noqa: E402

from veloce import Request, Veloce  # noqa: E402
from veloce.contrib.redis import RedisRateLimiter, RedisSessionStore  # noqa: E402
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


# ── RedisRateLimiter ─────────────────────────────────────────────────


def _app_with_limiter(client, **kwargs) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(RedisRateLimiter(client, **kwargs))

    @app.get("/")
    async def index(request: Request):
        return {"ok": True}

    return app


def test_rate_limiter_allows_under_limit(client):
    app = _app_with_limiter(client, max_requests=3, window_seconds=60)
    with TestClient(app) as tc:
        for _ in range(3):
            assert tc.get("/").status_code == 200


def test_rate_limiter_rejects_over_limit(client):
    app = _app_with_limiter(client, max_requests=2, window_seconds=60)
    with TestClient(app) as tc:
        assert tc.get("/").status_code == 200
        assert tc.get("/").status_code == 200
        resp = tc.get("/")
        assert resp.status_code == 429
        assert resp.headers["Retry-After"]
        assert resp.headers["X-RateLimit-Remaining"] == "0"


def test_rate_limiter_shares_state_across_instances(client):
    # Two limiter instances backed by the same Redis enforce one shared count -
    # the cross-worker property the in-process limiter lacks.
    app1 = _app_with_limiter(client, max_requests=2, window_seconds=60)
    app2 = _app_with_limiter(client, max_requests=2, window_seconds=60)
    with TestClient(app1) as tc1, TestClient(app2) as tc2:
        assert tc1.get("/").status_code == 200
        assert tc2.get("/").status_code == 200
        # The shared counter is now at the limit regardless of which app serves.
        assert tc1.get("/").status_code == 429


def test_rate_limiter_rejects_bad_config(client):
    with pytest.raises(ValueError, match="max_requests"):
        RedisRateLimiter(client, max_requests=0)
    with pytest.raises(ValueError, match="window_seconds"):
        RedisRateLimiter(client, window_seconds=0)
