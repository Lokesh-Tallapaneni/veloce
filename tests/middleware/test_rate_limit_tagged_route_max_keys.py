"""A `@rate_limit`-tagged route honours the operator's `max_keys` bound.

`RateLimitMiddleware(max_requests=..., max_keys=5_000)` validates and stores the
bound, and the sliding log honours it. But the backend built lazily for a
`@rate_limit`-tagged route was constructed as `InMemoryRateLimitBackend()` with
no arguments, so that route's keyspace grew to the 100,000 default - twenty times
the stated bound, reachable by cycling source addresses against the one tagged
route.

The `strategy=` arm of the same constructor passes
`InMemoryRateLimitBackend(max_keys=max_keys)` correctly, so the two arms
disagreed about whether `max_keys` means anything.
"""

from __future__ import annotations

import pytest

from veloce import ProxyFix, RateLimitMiddleware, TokenBucket, Veloce, rate_limit
from veloce.testclient import TestClient

BOUND = 12


def _app(max_keys: int = BOUND) -> Veloce:
    app = Veloce(openapi_url=None)
    # `ProxyFix` so `X-Forwarded-For` actually moves `request.client_host`, which
    # is what the bucket key is built from. Without it every request in a test
    # shares one key and a keyspace assertion passes on any bound at all.
    app.add_middleware(ProxyFix(x_for=1))
    app.add_middleware(RateLimitMiddleware(max_requests=60, window_seconds=60, max_keys=max_keys))

    @app.get("/tagged")
    @rate_limit(TokenBucket(rate=1000, per=1.0))
    async def tagged():
        return {"ok": True}

    @app.get("/plain")
    async def plain():
        return {"ok": True}

    return app


def _middleware(app: Veloce) -> RateLimitMiddleware:
    return next(m for m in app._middlewares if isinstance(m, RateLimitMiddleware))


def _drive(app: Veloce, path: str, count: int) -> None:
    """Send `count` requests from distinct source addresses."""
    client = TestClient(app)
    for i in range(count):
        client.get(path, headers={"X-Forwarded-For": f"10.0.{i // 256}.{i % 256}"})


def test_the_tagged_backend_carries_the_configured_bound():
    """The regression, read off the backend the tagged route built."""
    app = _app()
    _drive(app, "/tagged", 1)

    backend = _middleware(app)._backend

    assert backend is not None, "the tagged route built no backend"
    assert backend._max_keys == BOUND


def test_the_tagged_keyspace_stays_within_the_bound():
    """The consequence, driven rather than inspected.

    Four times the bound in distinct source addresses, so the assertion is only
    satisfiable by the cap actually holding.
    """
    app = _app()

    _drive(app, "/tagged", BOUND * 4)

    held = len(_middleware(app)._backend._states)

    assert held == BOUND, f"{held} keys held against a bound of {BOUND}"


def test_the_strategy_arm_still_carries_it():
    """The arm that was already correct, so the fix cannot swap the defect over."""
    app = Veloce(openapi_url=None)
    app.add_middleware(
        RateLimitMiddleware(strategy=TokenBucket(rate=1000, per=1.0), max_keys=BOUND)
    )

    @app.get("/x")
    async def x():
        return {"ok": True}

    assert _middleware(app)._backend._max_keys == BOUND


def test_the_default_bound_is_unchanged():
    """An operator who set nothing must still get the documented default."""
    app = Veloce(openapi_url=None)
    app.add_middleware(RateLimitMiddleware(max_requests=60, window_seconds=60))

    @app.get("/tagged")
    @rate_limit(TokenBucket(rate=1000, per=1.0))
    async def tagged():
        return {"ok": True}

    _drive(app, "/tagged", 1)

    assert _middleware(app)._backend._max_keys == 100_000


def test_the_tagged_route_still_limits():
    """Bounding the keyspace must not stop the limiter limiting."""
    app = Veloce(openapi_url=None)
    app.add_middleware(RateLimitMiddleware(max_requests=60, window_seconds=60, max_keys=BOUND))

    @app.get("/tagged")
    @rate_limit(TokenBucket(rate=2, per=3600.0))
    async def tagged():
        return {"ok": True}

    client = TestClient(app)
    statuses = [client.get("/tagged").status_code for _ in range(4)]

    assert statuses[:2] == [200, 200]
    assert 429 in statuses


@pytest.mark.parametrize("bound", [1, 5, 50])
def test_every_bound_reaches_the_tagged_backend(bound: int):
    app = _app(bound)
    _drive(app, "/tagged", 1)

    assert _middleware(app)._backend._max_keys == bound
