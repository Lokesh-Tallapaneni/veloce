"""Rate-limit algorithms, backends, and the RateLimitMiddleware strategy path."""

from __future__ import annotations

import pytest

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
