"""The default `RateLimitMiddleware()` bounds how many client keys it holds.

`RateLimitMiddleware(strategy=...)` evaluates through `InMemoryRateLimitBackend`,
whose docstring says "State is size-bounded to cap memory across many client
keys" and which caps at `max_keys=100_000`. The `max_requests=` constructor - the
one the guide reaches for first, and the one you get from a bare
`RateLimitMiddleware()` - kept its own `dict[str, deque[float]]` with no such
bound.

It swept stale buckets, but only once per `window_seconds`, and only those whose
newest stamp had aged out. Distinct keys arriving *within* a window accumulated
without limit:

    30,000 requests from 30,000 addresses in a /64  ->  len(_buckets) == 30000

which is a single machine's worth of IPv6 source addresses. The bound is now the
same on both constructors, with the same `max_keys` name and the same eviction
order - expired first, then oldest by insertion - so there is one thing to learn
rather than two.

The tests assert both constructors against each other where the behaviour should
match, and pin the eviction policy directly, because a bound that evicted the
*newest* key would let an attacker keep flushing an honest client's counter.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce import InMemoryRateLimitBackend, RateLimitMiddleware, TokenBucket, Veloce


def _app(mw) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(mw)

    @app.get("/")
    async def index():
        return {"ok": True}

    return app


async def _hit(app, hosts):
    async def send(message):
        pass

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    for host in hosts:
        await app(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": (host, 1),
                "scheme": "http",
                "server": ("t", 80),
                "http_version": "1.1",
                "root_path": "",
            },
            receive,
            send,
        )


def _drive(mw, count, prefix="2001:db8::"):
    asyncio.run(_hit(_app(mw), [f"{prefix}{i:x}" for i in range(count)]))
    return mw


# ── the bound, on the legacy constructor ─────────────────────────────


def test_the_legacy_constructor_bounds_its_bucket_dict():
    """The defect: 30,000 addresses produced 30,000 live buckets."""
    mw = _drive(RateLimitMiddleware(max_requests=1000, window_seconds=60, max_keys=64), 4000)
    assert len(mw._buckets) <= 64


def test_a_bare_middleware_is_bounded_by_default():
    """`RateLimitMiddleware()` with no arguments must not be the unbounded one."""
    mw = _drive(RateLimitMiddleware(max_requests=10_000), 300)
    assert mw._max_keys > 0
    assert len(mw._buckets) <= mw._max_keys


def test_the_default_bound_matches_the_strategy_backends():
    """One number to learn, not two."""
    assert RateLimitMiddleware()._max_keys == 100_000


def test_the_bound_is_configurable():
    mw = _drive(RateLimitMiddleware(max_requests=1000, max_keys=8), 200)
    assert len(mw._buckets) <= 8


def test_a_bound_below_one_is_refused():
    with pytest.raises(ValueError):
        RateLimitMiddleware(max_keys=0)


def test_a_negative_bound_is_refused():
    with pytest.raises(ValueError):
        RateLimitMiddleware(max_keys=-1)


# ── limiting still works while bounded ───────────────────────────────
#
# The negatives. A bound that broke enforcement would trade a memory problem for
# an availability one.


def test_a_single_client_is_still_limited_under_the_bound():
    mw = RateLimitMiddleware(max_requests=3, window_seconds=60, max_keys=8)
    app = _app(mw)
    asyncio.run(_hit(app, ["10.0.0.1"] * 3))
    assert len(mw._buckets["host:10.0.0.1"]) == 3


def test_a_client_that_exceeds_the_limit_is_refused():
    mw = RateLimitMiddleware(max_requests=2, window_seconds=60, max_keys=8)
    app = _app(mw)
    statuses = []

    async def run():
        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        for _ in range(4):
            await app(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/",
                    "raw_path": b"/",
                    "query_string": b"",
                    "headers": [],
                    "client": ("10.0.0.9", 1),
                    "scheme": "http",
                    "server": ("t", 80),
                    "http_version": "1.1",
                    "root_path": "",
                },
                receive,
                send,
            )

    asyncio.run(run())
    assert statuses == [200, 200, 429, 429]


def test_the_most_recent_client_keeps_its_counter_after_eviction():
    """Eviction must drop the oldest, never the arrival that triggered it.

    Evicting the newest would let an attacker flush an honest client's counter
    by cycling addresses - a rate-limit bypass dressed as a memory bound.
    """
    mw = RateLimitMiddleware(max_requests=100, window_seconds=60, max_keys=4)
    app = _app(mw)
    asyncio.run(_hit(app, [f"10.0.0.{i}" for i in range(20)]))
    assert "host:10.0.0.19" in mw._buckets


def test_eviction_keeps_the_dict_at_the_bound_not_empty():
    """A bound that cleared everything would reset every counter at once."""
    mw = _drive(RateLimitMiddleware(max_requests=100, max_keys=16), 100)
    assert 1 <= len(mw._buckets) <= 16


# ── the strategy constructor is unchanged ────────────────────────────


def test_the_strategy_path_is_still_bounded():
    mw = RateLimitMiddleware(strategy=TokenBucket(rate=1000, per=60))
    _drive(mw, 500)
    assert len(mw._backend._states) <= 100_000


def test_max_keys_reaches_the_default_strategy_backend():
    """The same knob, the same meaning, on both constructors."""
    mw = RateLimitMiddleware(strategy=TokenBucket(rate=1000, per=60), max_keys=32)
    _drive(mw, 400)
    assert len(mw._backend._states) <= 32


def test_max_keys_is_refused_with_an_explicit_backend():
    """Two places to set one bound is a configuration trap; say so."""
    with pytest.raises(ValueError):
        RateLimitMiddleware(
            strategy=TokenBucket(rate=10, per=60),
            backend=InMemoryRateLimitBackend(max_keys=5),
            max_keys=32,
        )


def test_an_explicit_backend_keeps_its_own_bound():
    mw = RateLimitMiddleware(
        strategy=TokenBucket(rate=1000, per=60),
        backend=InMemoryRateLimitBackend(max_keys=8),
    )
    _drive(mw, 200)
    assert len(mw._backend._states) <= 8
