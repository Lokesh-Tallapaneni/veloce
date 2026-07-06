"""Rate-limit algorithms, backends, and the RateLimitMiddleware strategy path."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import (
    FixedWindow,
    InMemoryRateLimitBackend,
    RateLimitBackend,
    RateLimitMiddleware,
    RateLimitResult,
    RateLimitStrategy,
    Request,
    SlidingWindow,
    TokenBucket,
    Veloce,
)
from veloce.testclient import TestClient

# ── FixedWindow ──────────────────────────────────────────────────────


def test_fixed_window_allows_then_rejects():
    strategy = FixedWindow(2, 60)
    state = None
    allowed = []
    for _ in range(3):
        result, state, _ttl = strategy.evaluate(state, 10.0)
        allowed.append(result.allowed)
    assert allowed == [True, True, False]


def test_fixed_window_resets_on_new_window():
    strategy = FixedWindow(1, 60)
    result, state, _ = strategy.evaluate(None, 10.0)
    assert result.allowed
    # Same window: second request rejected.
    result, state, _ = strategy.evaluate(state, 20.0)
    assert not result.allowed
    assert result.retry_after == 60 - 20
    # Next window: counter resets.
    result, state, _ = strategy.evaluate(state, 70.0)
    assert result.allowed


def test_fixed_window_rejects_bad_config():
    with pytest.raises(ValueError, match="limit"):
        FixedWindow(0)
    with pytest.raises(ValueError, match="window"):
        FixedWindow(1, 0)


# ── SlidingWindow ────────────────────────────────────────────────────


def test_sliding_window_allows_then_rejects():
    strategy = SlidingWindow(2, 60)
    state = None
    allowed = []
    for _ in range(3):
        result, state, _ = strategy.evaluate(state, 0.0)
        allowed.append(result.allowed)
    assert allowed == [True, True, False]


def test_sliding_window_weights_previous_window():
    strategy = SlidingWindow(2, 60)
    # Fill the first window (window 0).
    _, state, _ = strategy.evaluate(None, 0.0)
    _, state, _ = strategy.evaluate(state, 0.0)
    # At the next window boundary the previous count is weighted fully, so the
    # rolling estimate still meets the limit and rejects.
    result, state, _ = strategy.evaluate(state, 60.0)
    assert not result.allowed
    # Late in the next window the previous window's weight has decayed, allowing
    # the request through.
    result, state, _ = strategy.evaluate(state, 119.0)
    assert result.allowed


def test_sliding_window_resets_after_multi_window_gap():
    strategy = SlidingWindow(1, 60)
    # Use up window 0, then evaluate several windows later: the stale state is
    # more than one window old, so both counters reset and the request is allowed.
    _, state, _ = strategy.evaluate(None, 0.0)
    result, _state, _ = strategy.evaluate(state, 300.0)
    assert result.allowed


def test_sliding_window_rejects_bad_config():
    with pytest.raises(ValueError, match="limit"):
        SlidingWindow(0)
    with pytest.raises(ValueError, match="window"):
        SlidingWindow(1, 0)


# ── TokenBucket ──────────────────────────────────────────────────────


def test_token_bucket_allows_burst_then_rejects():
    strategy = TokenBucket(rate=2, per=60, burst=2)
    state = None
    allowed = []
    for _ in range(3):
        result, state, _ = strategy.evaluate(state, 0.0)
        allowed.append(result.allowed)
    assert allowed == [True, True, False]


def test_token_bucket_refills_over_time():
    strategy = TokenBucket(rate=2, per=60, burst=2)
    state = None
    for _ in range(3):
        result, state, _ = strategy.evaluate(state, 0.0)
    assert not result.allowed
    # A full window later the bucket has refilled to its burst capacity.
    result, state, _ = strategy.evaluate(state, 60.0)
    assert result.allowed


def test_token_bucket_default_burst_is_rate():
    strategy = TokenBucket(rate=5, per=1.0)
    assert strategy.burst == 5


def test_token_bucket_leaky_with_burst_one():
    # burst=1 is the strict leaky-bucket shape: no bursting beyond one token.
    strategy = TokenBucket(rate=1, per=10, burst=1)
    result, state, _ = strategy.evaluate(None, 0.0)
    assert result.allowed
    result, state, _ = strategy.evaluate(state, 0.0)
    assert not result.allowed


def test_token_bucket_rejects_bad_config():
    with pytest.raises(ValueError, match="rate"):
        TokenBucket(0)
    with pytest.raises(ValueError, match="per"):
        TokenBucket(1, per=0)
    with pytest.raises(ValueError, match="burst"):
        TokenBucket(1, burst=0)


# ── InMemoryRateLimitBackend ─────────────────────────────────────────


async def test_backend_evaluate_runs_strategy():
    backend = InMemoryRateLimitBackend()
    strategy = FixedWindow(1, 60)
    first = await backend.evaluate("client", strategy, 0.0)
    second = await backend.evaluate("client", strategy, 0.0)
    assert first.allowed
    assert not second.allowed


async def test_backend_isolates_keys():
    backend = InMemoryRateLimitBackend()
    strategy = FixedWindow(1, 60)
    assert (await backend.evaluate("a", strategy, 0.0)).allowed
    # A different client has its own counter.
    assert (await backend.evaluate("b", strategy, 0.0)).allowed


async def test_backend_expires_stale_state():
    backend = InMemoryRateLimitBackend()
    strategy = FixedWindow(1, 60)
    assert (await backend.evaluate("c", strategy, 0.0)).allowed
    # Past the stored TTL the state is treated as absent and the counter resets.
    assert (await backend.evaluate("c", strategy, 1000.0)).allowed


async def test_backend_is_size_bounded():
    backend = InMemoryRateLimitBackend(max_keys=2)
    strategy = FixedWindow(5, 60)
    for key in ("a", "b", "c", "d"):
        await backend.evaluate(key, strategy, 0.0)
    assert len(backend._states) <= 2


async def test_backend_evicts_expired_before_oldest():
    backend = InMemoryRateLimitBackend(max_keys=2)
    strategy = FixedWindow(5, 60)
    await backend.evaluate("a", strategy, 0.0)
    await backend.evaluate("b", strategy, 0.0)
    # Past their TTL, evaluating a new key evicts the expired entries first.
    await backend.evaluate("c", strategy, 1000.0)
    assert "a" not in backend._states
    assert "b" not in backend._states


def test_backend_rejects_bad_config():
    with pytest.raises(ValueError, match="max_keys"):
        InMemoryRateLimitBackend(max_keys=0)


# ── Base classes ─────────────────────────────────────────────────────


def test_strategy_base_is_abstract():
    with pytest.raises(NotImplementedError):
        RateLimitStrategy().evaluate(None, 0.0)


async def test_backend_base_is_abstract():
    with pytest.raises(NotImplementedError):
        await RateLimitBackend().evaluate("k", FixedWindow(1), 0.0)


def test_strategy_subclass_requires_slots():
    with pytest.raises(TypeError, match="__slots__"):

        class BadStrategy(RateLimitStrategy):  # no __slots__
            pass


def test_backend_subclass_requires_slots():
    with pytest.raises(TypeError, match="__slots__"):

        class BadBackend(RateLimitBackend):  # no __slots__
            pass


def test_result_fields():
    result = RateLimitResult(allowed=True, limit=10, remaining=9, retry_after=0, reset=42)
    assert result.allowed and result.limit == 10 and result.remaining == 9
    assert result.reset == 42


# ── RateLimitMiddleware strategy path ────────────────────────────────

# A stable User-Agent so the per-client bucket key is deterministic under
# TestClient (which carries no transport peer address).
_UA = {"User-Agent": "rl-test"}


def _app(strategy, backend=None) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(RateLimitMiddleware(strategy=strategy, backend=backend))

    @app.get("/")
    async def index(request: Request):
        return {"ok": True}

    return app


def test_middleware_strategy_allows_and_sets_headers():
    with TestClient(_app(FixedWindow(5, 60))) as tc:
        resp = tc.get("/", headers=_UA)
        assert resp.status_code == 200
        assert resp.headers["X-RateLimit-Limit"] == "5"
        assert int(resp.headers["X-RateLimit-Remaining"]) >= 0
        # Reset reports real seconds-until-replenish, not 0, on an allowed
        # response (the legacy path does the same).
        assert int(resp.headers["X-RateLimit-Reset"]) > 0


def test_middleware_strategy_rejects_over_limit():
    with TestClient(_app(FixedWindow(2, 60))) as tc:
        assert tc.get("/", headers=_UA).status_code == 200
        assert tc.get("/", headers=_UA).status_code == 200
        resp = tc.get("/", headers=_UA)
        assert resp.status_code == 429
        assert resp.headers["Retry-After"]
        assert resp.headers["X-RateLimit-Remaining"] == "0"


def test_middleware_token_bucket_path():
    with TestClient(_app(TokenBucket(rate=2, per=60, burst=2))) as tc:
        assert tc.get("/", headers=_UA).status_code == 200
        assert tc.get("/", headers=_UA).status_code == 200
        assert tc.get("/", headers=_UA).status_code == 429


def test_middleware_rejects_backend_without_strategy():
    with pytest.raises(ValueError, match="strategy"):
        RateLimitMiddleware(backend=InMemoryRateLimitBackend())


def test_middleware_legacy_path_still_works():
    # The released max_requests/window_seconds signature is unchanged.
    app = Veloce(openapi_url=None)
    app.add_middleware(RateLimitMiddleware(max_requests=2, window_seconds=60))

    @app.get("/")
    async def index(request: Request):
        return {"ok": True}

    with TestClient(app) as tc:
        assert tc.get("/", headers=_UA).status_code == 200
        assert tc.get("/", headers=_UA).status_code == 200
        assert tc.get("/", headers=_UA).status_code == 429


# ── Per-route overrides ──────────────────────────────────────────────


def _app_routes(strategy, overrides=None) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(RateLimitMiddleware(strategy=strategy, overrides=overrides))

    @app.get("/cheap")
    async def cheap(request: Request):
        return {"ok": True}

    @app.get("/strict")
    async def strict(request: Request):
        return {"ok": True}

    return app


def test_override_route_uses_its_own_limit():
    app = _app_routes(FixedWindow(100, 60), overrides={"/strict": FixedWindow(2, 60)})
    with TestClient(app) as tc:
        assert tc.get("/strict", headers=_UA).status_code == 200
        assert tc.get("/strict", headers=_UA).status_code == 200
        assert tc.get("/strict", headers=_UA).status_code == 429
        # The non-overridden route keeps the generous default.
        assert tc.get("/cheap", headers=_UA).status_code == 200


def test_override_route_counter_is_independent_of_default():
    # Exhausting the default budget must not throttle an overridden route -
    # separate per-route counters via the route-scoped key.
    app = _app_routes(FixedWindow(1, 60), overrides={"/strict": FixedWindow(5, 60)})
    with TestClient(app) as tc:
        assert tc.get("/cheap", headers=_UA).status_code == 200
        assert tc.get("/cheap", headers=_UA).status_code == 429
        assert tc.get("/strict", headers=_UA).status_code == 200


def test_overrides_require_strategy():
    with pytest.raises(ValueError, match="strategy"):
        RateLimitMiddleware(overrides={"/x": FixedWindow(1)})


def test_overrides_reject_non_strategy():
    with pytest.raises(TypeError, match="RateLimitStrategy"):
        RateLimitMiddleware(strategy=FixedWindow(10), overrides={"/x": "nope"})


def _req(app, path):
    return Request(method="GET", path=path, query_string="", headers={}, body=b"", app=app)


def test_unknown_override_key_raises_on_first_request():
    app = Veloce(openapi_url=None)

    @app.get("/cheap")
    async def cheap(request: Request):
        return {}

    mw = RateLimitMiddleware(strategy=FixedWindow(10), overrides={"/nope": FixedWindow(1)})
    with pytest.raises(ValueError, match="match no registered route"):
        mw._build_route_strategies(_req(app, "/cheap"))


def test_valid_override_key_passes_validation():
    app = Veloce(openapi_url=None)

    @app.get("/strict")
    async def strict(request: Request):
        return {}

    mw = RateLimitMiddleware(strategy=FixedWindow(10), overrides={"/strict": FixedWindow(1)})
    built = mw._build_route_strategies(_req(app, "/strict"))
    assert "/strict" in built
    assert mw._route_strategies is built


def test_blueprint_override_key_needs_prefix():
    from veloce import Blueprint

    app = Veloce(openapi_url=None)
    bp = Blueprint("api", url_prefix="/api")

    @bp.get("/login")
    async def login(request: Request):
        return {}

    app.register_blueprint(bp)
    # The bare "/login" matches no route; the prefixed "/api/login" does.
    bad = RateLimitMiddleware(strategy=FixedWindow(10), overrides={"/login": FixedWindow(1)})
    with pytest.raises(ValueError, match="match no registered route"):
        bad._build_route_strategies(_req(app, "/api/login"))
    ok = RateLimitMiddleware(strategy=FixedWindow(10), overrides={"/api/login": FixedWindow(1)})
    assert "/api/login" in ok._build_route_strategies(_req(app, "/api/login"))


# ── @rate_limit decorator ────────────────────────────────────────────


def test_rate_limit_decorator_tags_handler():
    from veloce import rate_limit
    from veloce.ratelimit import RATE_LIMIT_ATTR

    strat = FixedWindow(5, 60)

    @rate_limit(strat)
    async def handler(request):
        return {}

    assert getattr(handler, RATE_LIMIT_ATTR) is strat


def test_rate_limit_decorator_requires_strategy():
    from veloce import rate_limit

    with pytest.raises(TypeError, match="RateLimitStrategy"):
        rate_limit("nope")


def test_rate_limit_decorator_applies_per_route():
    from veloce import rate_limit

    app = Veloce(openapi_url=None)
    app.add_middleware(RateLimitMiddleware(strategy=FixedWindow(100, 60)))

    @app.get("/cheap")
    async def cheap(request: Request):
        return {"ok": True}

    @app.get("/strict")
    @rate_limit(FixedWindow(2, 60))
    async def strict(request: Request):
        return {"ok": True}

    with TestClient(app) as tc:
        assert tc.get("/strict", headers=_UA).status_code == 200
        assert tc.get("/strict", headers=_UA).status_code == 200
        assert tc.get("/strict", headers=_UA).status_code == 429
        # The undecorated route keeps the generous default.
        assert tc.get("/cheap", headers=_UA).status_code == 200


def test_rate_limit_decorator_applies_to_hidden_route():
    # A `@rate_limit` tag on an `include_in_schema=False` route (a login form
    # POST, an internal endpoint - the routes most worth throttling) must still
    # be enforced. The strategy scan walks hidden routes too, so the tag is not
    # silently dropped with the schema-only view.
    from veloce import rate_limit

    app = Veloce(openapi_url=None)
    app.add_middleware(RateLimitMiddleware(strategy=FixedWindow(100, 60)))

    @app.post("/login", include_in_schema=False)
    @rate_limit(FixedWindow(2, 60))
    async def login(request: Request):
        return {"ok": True}

    with TestClient(app) as tc:
        assert tc.post("/login", headers=_UA).status_code == 200
        assert tc.post("/login", headers=_UA).status_code == 200
        assert tc.post("/login", headers=_UA).status_code == 429


def test_route_added_after_first_request_is_limited():
    # A route registered after the per-route cache was primed must still be
    # picked up (the cache rebuilds when the app's route generation advances).
    from veloce import rate_limit

    app = Veloce(openapi_url=None)
    app.add_middleware(RateLimitMiddleware(strategy=FixedWindow(100, 60)))

    @app.get("/early")
    async def early(request: Request):
        return {"ok": True}

    with TestClient(app) as tc:
        assert tc.get("/early", headers=_UA).status_code == 200  # primes the cache

        @app.get("/late")
        @rate_limit(FixedWindow(1, 60))
        async def late(request: Request):
            return {"ok": True}

        assert tc.get("/late", headers=_UA).status_code == 200
        assert tc.get("/late", headers=_UA).status_code == 429


def test_explicit_override_wins_over_decorator():
    from veloce import rate_limit

    app = Veloce(openapi_url=None)
    # Decorator says 100/min, overrides says 1/min - the explicit map wins.
    app.add_middleware(
        RateLimitMiddleware(
            strategy=FixedWindow(1000, 60),
            overrides={"/strict": FixedWindow(1, 60)},
        )
    )

    @app.get("/strict")
    @rate_limit(FixedWindow(100, 60))
    async def strict(request: Request):
        return {"ok": True}

    with TestClient(app) as tc:
        assert tc.get("/strict", headers=_UA).status_code == 200
        assert tc.get("/strict", headers=_UA).status_code == 429


class TestRateLimitMiddlewareE2E:
    @pytest.mark.asyncio
    async def test_rate_limit(self):
        app = Veloce(openapi_url=None)
        app.add_middleware(RateLimitMiddleware(max_requests=2, window_seconds=60))

        @app.get("/")
        async def index(request: Request):
            return {"ok": True}

        # Stable UA → all three requests bucket together (no client_host
        # in these synthetic Requests; UA hash is the next fallback).
        ua = {"user-agent": "rate-limit-test/1.0"}

        # First two requests should pass
        for _ in range(2):
            resp = await app.handle_request(make_request(headers=ua))
            assert resp.status_code == 200

        # Third should be rate limited
        resp = await app.handle_request(make_request(headers=ua))
        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_rate_limit_headers_on_success(self):
        """Successful responses carry X-RateLimit-Limit/Remaining/Reset."""
        app = Veloce(openapi_url=None)
        app.add_middleware(RateLimitMiddleware(max_requests=5, window_seconds=60))

        @app.get("/")
        async def index(request: Request):
            return {"ok": True}

        ua = {"user-agent": "rl-headers-success/1.0"}
        resp = await app.handle_request(make_request(headers=ua))
        assert resp.status_code == 200
        assert resp.headers["X-RateLimit-Limit"] == "5"
        assert resp.headers["X-RateLimit-Remaining"] == "4"
        # Pin the seconds-remaining form — a unix epoch would also satisfy
        # >= 0 and silently regress the header semantics. The upper bound is
        # window + 1: `_reset_after` ceils a sub-second remainder, so a fresh
        # window can momentarily round up to `window_seconds + 1`.
        assert 0 <= int(resp.headers["X-RateLimit-Reset"]) <= 61

        resp = await app.handle_request(make_request(headers=ua))
        assert resp.headers["X-RateLimit-Remaining"] == "3"

    @pytest.mark.asyncio
    async def test_rate_limit_headers_on_429(self):
        """A 429 carries X-RateLimit-* plus Retry-After."""
        app = Veloce(openapi_url=None)
        app.add_middleware(RateLimitMiddleware(max_requests=1, window_seconds=60))

        @app.get("/")
        async def index(request: Request):
            return {"ok": True}

        ua = {"user-agent": "rl-headers-429/1.0"}
        ok = await app.handle_request(make_request(headers=ua))
        assert ok.status_code == 200

        rejected = await app.handle_request(make_request(headers=ua))
        assert rejected.status_code == 429
        assert rejected.headers["X-RateLimit-Limit"] == "1"
        assert rejected.headers["X-RateLimit-Remaining"] == "0"
        # Pin the seconds-remaining form — a unix epoch would also satisfy
        # >= 0 and silently regress the header semantics.
        assert 0 <= int(rejected.headers["X-RateLimit-Reset"]) <= 60
        assert "Retry-After" in rejected.headers

    @pytest.mark.asyncio
    async def test_clientless_requests_do_not_share_bucket(self):
        """Two anonymous requests with no shared signals must not collide."""
        app = Veloce(openapi_url=None)
        app.add_middleware(RateLimitMiddleware(max_requests=1, window_seconds=60))

        @app.get("/")
        async def index(request: Request):
            return {"ok": True}

        # Distinct scope identity per request + no UA + no XFF → distinct
        # buckets. The legacy "unknown" key would 429 the second call.
        r1 = await app.handle_request(make_request())
        assert r1.status_code == 200
        r2 = await app.handle_request(make_request())
        assert r2.status_code == 200


async def test_rate_limit_middleware_evicts_stale_buckets():
    """The bucket dict must not grow unbounded with unique client IPs."""
    import time as _time

    mw = RateLimitMiddleware(max_requests=1000, window_seconds=1)
    now = _time.monotonic()
    stale = now - 3600
    mw._buckets = {f"stale-{i}": [stale] for i in range(100)}
    mw._buckets["fresh"] = [now]  # a live bucket — must survive the sweep
    mw._last_sweep = stale  # force the next request to trigger a sweep

    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    await mw.process_request(req)

    # The 100 stale buckets are evicted; the live bucket is kept.
    assert not any(k.startswith("stale-") for k in mw._buckets)
    assert "fresh" in mw._buckets
