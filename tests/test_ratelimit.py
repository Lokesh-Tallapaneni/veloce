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


def test_unknown_override_key_is_reported_not_raised_on_a_request():
    """It used to raise here, so silencing the startup finding - the documented
    way of accepting it - turned every request into a 500 instead."""
    from veloce.audit import run

    app = Veloce(openapi_url=None)

    @app.get("/cheap")
    async def cheap(request: Request):
        return {}

    mw = RateLimitMiddleware(strategy=FixedWindow(10), overrides={"/nope": FixedWindow(1)})
    app.add_middleware(mw)
    # The request path reports; the audit is what decides it is fatal.
    assert mw._build_route_strategies(_req(app, "/cheap")) is not None
    assert [
        f.severity for f in run(app, routes_final=True) if f.id == "ratelimit-overrides-unknown"
    ] == ["error"]


def test_valid_override_key_passes_validation():
    app = Veloce(openapi_url=None)

    @app.get("/strict")
    async def strict(request: Request):
        return {}

    mw = RateLimitMiddleware(strategy=FixedWindow(10), overrides={"/strict": FixedWindow(1)})
    built = mw._build_route_strategies(_req(app, "/strict"))
    assert "/strict" in built
    assert mw._route_strategies is built


def test_unknown_override_key_fails_startup_not_every_request():
    # An override key matching no route must fail the boot with the naming
    # error, not surface as a 500 on each request once traffic arrives.
    app = Veloce(openapi_url=None)

    @app.get("/cheap")
    async def cheap(request: Request):
        return {}

    app.add_middleware(
        RateLimitMiddleware(strategy=FixedWindow(10), overrides={"/nope": FixedWindow(1)})
    )
    with pytest.raises(ValueError, match="match no registered route"):
        TestClient(app)


def test_valid_override_key_starts_up_cleanly():
    app = Veloce(openapi_url=None)

    @app.get("/strict")
    async def strict(request: Request):
        return {}

    app.add_middleware(
        RateLimitMiddleware(strategy=FixedWindow(10), overrides={"/strict": FixedWindow(5, 60)})
    )
    with TestClient(app) as tc:
        assert tc.get("/strict", headers=_UA).status_code == 200


def test_blueprint_override_key_needs_prefix():
    from veloce import Blueprint

    app = Veloce(openapi_url=None)
    bp = Blueprint("api", url_prefix="/api")

    @bp.get("/login")
    async def login(request: Request):
        return {}

    app.register_blueprint(bp)
    # The bare "/login" matches no route; the prefixed "/api/login" does.
    from veloce.audit import run

    bad = RateLimitMiddleware(strategy=FixedWindow(10), overrides={"/login": FixedWindow(1)})
    bad_app = Veloce(openapi_url=None)
    bad_app.register_blueprint(bp)
    bad_app.add_middleware(bad)
    finding = next(
        f for f in run(bad_app, routes_final=True) if f.id == "ratelimit-overrides-unknown"
    )
    assert "/login" in str(finding)
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


def test_reset_never_exceeds_the_window():
    # `X-RateLimit-Reset` describes a wait inside the window, so it must never
    # advertise longer than the window itself - a coarse clock can otherwise
    # round the remainder past it.
    from collections import deque

    mw = RateLimitMiddleware(max_requests=1, window_seconds=60)
    now = 1000.0
    # Oldest stamp equal to (and, defensively, later than) `now`.
    assert mw._reset_after(deque([now]), now) <= 60
    assert mw._reset_after(deque([now + 5]), now) <= 60
    # A partly-elapsed window still reports the real remainder.
    assert mw._reset_after(deque([now - 30]), now) == 30
    # An expired stamp reports nothing left to wait for.
    assert mw._reset_after(deque([now - 120]), now) == 0


def test_strict_overrides_false_warns_instead_of_failing_startup(caplog):
    # One app that mounts a different set of routers per deployment shares a
    # single override map, so an unmatched key must be skippable.
    app = Veloce(openapi_url=None)

    @app.get("/real")
    async def real(request: Request):
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware(
            strategy=FixedWindow(100, 60),
            overrides={"/real": FixedWindow(5, 60), "/not-mounted": FixedWindow(2, 60)},
            strict_overrides=False,
        )
    )
    with caplog.at_level("WARNING"), TestClient(app) as tc:
        assert tc.get("/real", headers=_UA).status_code == 200
    assert any("/not-mounted" in r.getMessage() for r in caplog.records)


def test_strict_overrides_defaults_to_failing_startup():
    app = Veloce(openapi_url=None)

    @app.get("/real")
    async def real(request: Request):
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware(
            strategy=FixedWindow(100, 60),
            overrides={"/not-mounted": FixedWindow(2, 60)},
        )
    )
    with pytest.raises(ValueError, match="match no registered route"):
        TestClient(app)


# ── @rate_limit in the max_requests= constructor mode ────────────────
#
# `rate_limit()` promises unconditionally that "a decorated handler is limited
# by `strategy` ... overriding the RateLimitMiddleware default". That held only
# when the middleware was built with `strategy=`. Built the other way -
# `RateLimitMiddleware(max_requests=..., window_seconds=...)`, the default
# shape - the tag was collected by nothing and dropped in silence:
#
#     app.add_middleware(RateLimitMiddleware(max_requests=1000, window_seconds=60))
#
#     @app.post("/login")
#     @rate_limit(FixedWindow(limit=2, window=60))
#     async def login(): ...      # -> 200, 200, 200, 200
#
# A strict limit on a sensitive route silently became no limit at all. The
# constructor already refuses `backend=` and `overrides=` without `strategy=`,
# so the one misconfiguration it did not report was the one that mattered.


def _tagged_app(**middleware_kwargs) -> Veloce:
    from veloce import rate_limit

    app = Veloce(openapi_url=None)
    app.add_middleware(RateLimitMiddleware(**middleware_kwargs))

    @app.get("/login")
    @rate_limit(FixedWindow(2, 60))
    async def login(request: Request):
        return {"ok": True}

    @app.get("/open")
    async def open_route(request: Request):
        return {"ok": True}

    return app


def test_tag_is_honored_in_the_max_requests_mode():
    """The defect: the decorator was dropped without a word."""
    with TestClient(_tagged_app(max_requests=1000, window_seconds=60)) as tc:
        codes = [tc.get("/login", headers=_UA).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]


def test_both_constructor_modes_agree():
    """The tag must mean the same thing whichever way the middleware was built."""
    with TestClient(_tagged_app(max_requests=1000, window_seconds=60)) as tc:
        legacy = [tc.get("/login", headers=_UA).status_code for _ in range(4)]
    with TestClient(_tagged_app(strategy=FixedWindow(1000, 60))) as tc:
        strategy = [tc.get("/login", headers=_UA).status_code for _ in range(4)]
    assert legacy == strategy


def test_an_untagged_route_keeps_the_default_budget():
    """Only the tagged route changes; everything else keeps the plain limit."""
    with TestClient(_tagged_app(max_requests=3, window_seconds=60)) as tc:
        codes = [tc.get("/open", headers=_UA).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_the_tagged_route_has_its_own_counter():
    """A tagged route must not spend, or be spent by, the default budget."""
    with TestClient(_tagged_app(max_requests=3, window_seconds=60)) as tc:
        for _ in range(3):
            assert tc.get("/open", headers=_UA).status_code == 200
        assert tc.get("/open", headers=_UA).status_code == 429
        # The default budget is exhausted; the tagged route has its own.
        assert tc.get("/login", headers=_UA).status_code == 200


def test_the_tagged_route_still_reports_its_headers():
    """X-RateLimit-* must describe the tag's budget, not the default."""
    with TestClient(_tagged_app(max_requests=1000, window_seconds=60)) as tc:
        response = tc.get("/login", headers=_UA)
    assert response.headers.get("X-RateLimit-Limit") == "2"
    assert response.headers.get("X-RateLimit-Remaining") == "1"


def test_a_refused_tagged_request_sends_retry_after():
    with TestClient(_tagged_app(max_requests=1000, window_seconds=60)) as tc:
        for _ in range(2):
            tc.get("/login", headers=_UA)
        refused = tc.get("/login", headers=_UA)
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0


async def test_distinct_clients_keep_distinct_tagged_buckets():
    """A shared bucket would let one caller exhaust everyone else's quota.

    Driven through the middleware directly: `TestClient` reports one peer for
    every request, and the peer outranks every other signal in `_bucket_key`,
    so the caller cannot be varied over the wire here.
    """
    app = _tagged_app(max_requests=1000, window_seconds=60)
    middleware = app._middlewares[-1]

    async def call(agent: str):
        request = make_request(path="/login", headers={"User-Agent": agent})
        request.app = app
        request._state["url_rule"] = "/login"
        return await middleware.process_request(request)

    assert await call("first") is None
    assert await call("first") is None
    refused = await call("first")
    assert refused is not None and refused.status_code == 429
    assert await call("second") is None


def test_a_route_added_after_the_first_request_is_tagged():
    """The route table can grow; the tag map is rebuilt on generation change."""
    from veloce import rate_limit

    app = Veloce(openapi_url=None)
    app.add_middleware(RateLimitMiddleware(max_requests=1000, window_seconds=60))

    @app.get("/first")
    async def first(request: Request):
        return {"ok": True}

    tc = TestClient(app)
    assert tc.get("/first", headers=_UA).status_code == 200

    @app.get("/late")
    @rate_limit(FixedWindow(1, 60))
    async def late(request: Request):
        return {"ok": True}

    assert tc.get("/late", headers=_UA).status_code == 200
    assert tc.get("/late", headers=_UA).status_code == 429


def test_the_untagged_common_path_is_unchanged():
    """No tagged route anywhere: the legacy sliding log must behave as before."""
    app = Veloce(openapi_url=None)
    app.add_middleware(RateLimitMiddleware(max_requests=2, window_seconds=60))

    @app.get("/plain")
    async def plain(request: Request):
        return {"ok": True}

    with TestClient(app) as tc:
        codes = [tc.get("/plain", headers=_UA).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
