"""Pluggable rate-limit algorithms and backends for `RateLimitMiddleware`.

A *strategy* is the algorithm - fixed window, sliding window, or token bucket -
expressed as a pure state transition: given the prior per-client state and the
current time it returns a decision, the next state, and how long that state stays
relevant. A *backend* is where that state lives - in this process
(`InMemoryRateLimitBackend`) or shared across workers
(`veloce.contrib.redis.RedisRateLimitBackend`). Because the algorithm is pure,
each one is written once and runs identically on either backend.

Select an algorithm by passing a strategy to `RateLimitMiddleware`::

    from veloce import RateLimitMiddleware, TokenBucket

    app.add_middleware(RateLimitMiddleware(strategy=TokenBucket(rate=100, per=60, burst=20)))
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Per-client algorithm state persisted between requests. It round-trips through
# the backend as JSON, so its values are plain numbers (ints widen to float).
RateLimitState = dict[str, float]


@dataclass(slots=True)
class RateLimitResult:
    """The outcome of evaluating one request against a strategy."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # seconds until the next request is allowed (0 when allowed)
    reset: int  # seconds until the limit replenishes (for X-RateLimit-Reset)


class RateLimitStrategy:
    """A rate-limit algorithm as a pure state transition.

    `evaluate` takes the client's prior state (`None` on the first request or
    after expiry) and the current wall-clock time, and returns the decision, the
    next state to persist, and a TTL in seconds after which that state can be
    dropped. It performs no I/O, so a backend can run it under its own atomicity
    (a dict mutation in-process, a watched transaction on Redis).

    Subclasses must declare ``__slots__`` (even ``__slots__ = ()``).
    """

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "__slots__" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} must declare __slots__ (even __slots__ = ())")

    def evaluate(
        self, state: RateLimitState | None, now: float
    ) -> tuple[RateLimitResult, RateLimitState, int]:
        raise NotImplementedError


class FixedWindow(RateLimitStrategy):
    """Allow `limit` requests per fixed `window` seconds.

    Simple and cheap, but a burst straddling a window boundary can admit up to
    ``2 x limit`` briefly - use `SlidingWindow` or `TokenBucket` when that
    matters.
    """

    __slots__ = ("limit", "window")

    def __init__(self, limit: int, window: int = 60) -> None:
        if limit < 1:
            raise ValueError("FixedWindow limit must be >= 1")
        if window < 1:
            raise ValueError("FixedWindow window must be >= 1 second")
        self.limit = limit
        self.window = window

    def evaluate(
        self, state: RateLimitState | None, now: float
    ) -> tuple[RateLimitResult, RateLimitState, int]:
        window = int(now // self.window)
        count = int(state["count"]) + 1 if state is not None and state["window"] == window else 1
        allowed = count <= self.limit
        remaining = max(0, self.limit - count)
        reset = self.window - int(now) % self.window
        retry_after = 0 if allowed else reset
        result = RateLimitResult(allowed, self.limit, remaining, retry_after, reset)
        return result, {"window": window, "count": count}, self.window


class SlidingWindow(RateLimitStrategy):
    """Allow `limit` requests per rolling `window` seconds.

    Weights the previous window's count by how far the current window has
    progressed, so the boundary burst `FixedWindow` allows is smoothed away while
    keeping only two counters of state.
    """

    __slots__ = ("limit", "window")

    def __init__(self, limit: int, window: int = 60) -> None:
        if limit < 1:
            raise ValueError("SlidingWindow limit must be >= 1")
        if window < 1:
            raise ValueError("SlidingWindow window must be >= 1 second")
        self.limit = limit
        self.window = window

    def evaluate(
        self, state: RateLimitState | None, now: float
    ) -> tuple[RateLimitResult, RateLimitState, int]:
        window = int(now // self.window)
        fraction = (now % self.window) / self.window
        if state is not None and state["window"] == window:
            prev, curr = state["prev"], state["curr"]
        elif state is not None and state["window"] == window - 1:
            prev, curr = state["curr"], 0
        else:
            prev, curr = 0, 0
        estimate = prev * (1 - fraction) + curr
        allowed = estimate < self.limit
        if allowed:
            curr += 1
        remaining = max(0, int(self.limit - (prev * (1 - fraction) + curr)))
        reset = self.window - int(now) % self.window
        retry_after = 0 if allowed else max(1, reset)
        result = RateLimitResult(allowed, self.limit, remaining, retry_after, reset)
        # Keep the previous window's count available into the next window.
        return result, {"window": window, "prev": prev, "curr": curr}, self.window * 2


class TokenBucket(RateLimitStrategy):
    """Refill `rate` tokens per `per` seconds, allowing bursts up to `burst`.

    Each request spends one token; an empty bucket rejects until it refills. The
    bucket tolerates a short burst (up to `burst`, default `rate`) while holding
    the long-run average to `rate`/`per`. A leaky-bucket-style strict limiter is
    `TokenBucket(rate, per, burst=1)`.
    """

    __slots__ = ("rate", "per", "burst", "_refill")

    def __init__(self, rate: int, per: float = 1.0, burst: int | None = None) -> None:
        if rate < 1:
            raise ValueError("TokenBucket rate must be >= 1")
        if per <= 0:
            raise ValueError("TokenBucket per must be > 0 seconds")
        if burst is not None and burst < 1:
            raise ValueError("TokenBucket burst must be >= 1")
        self.rate = rate
        self.per = per
        self.burst = burst if burst is not None else rate
        self._refill = rate / per  # tokens per second

    def evaluate(
        self, state: RateLimitState | None, now: float
    ) -> tuple[RateLimitResult, RateLimitState, int]:
        if state is not None:
            tokens = min(self.burst, state["tokens"] + (now - state["ts"]) * self._refill)
        else:
            tokens = float(self.burst)
        allowed = tokens >= 1
        if allowed:
            tokens -= 1
            retry_after = 0
        else:
            retry_after = max(1, math.ceil((1 - tokens) / self._refill))
        # Seconds for the bucket to refill back to its burst capacity.
        reset = math.ceil((self.burst - tokens) / self._refill) if tokens < self.burst else 0
        result = RateLimitResult(allowed, self.burst, int(tokens), retry_after, reset)
        # State stays relevant only until a drained bucket would fully refill.
        ttl = math.ceil(self.burst / self._refill) + 1
        return result, {"tokens": tokens, "ts": now}, ttl


class RateLimitBackend:
    """Where per-client rate-limit state lives, and the atomic read-modify-write.

    `evaluate` loads the state for `key`, runs `strategy.evaluate`, persists the
    next state with its TTL, and returns the decision - all atomically, so two
    concurrent requests for the same client cannot both read a stale count.

    Subclasses must declare ``__slots__`` (even ``__slots__ = ()``).
    """

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "__slots__" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} must declare __slots__ (even __slots__ = ())")

    async def evaluate(self, key: str, strategy: RateLimitStrategy, now: float) -> RateLimitResult:
        raise NotImplementedError


class InMemoryRateLimitBackend(RateLimitBackend):
    """Process-local rate-limit state - the default backend.

    Not shared across workers, so a multi-worker deployment enforces roughly
    ``N x`` the limit; use `veloce.contrib.redis.RedisRateLimitBackend` for one
    shared limit. State is size-bounded to cap memory across many client keys.
    """

    __slots__ = ("_states", "_max_keys")

    def __init__(self, max_keys: int = 100_000) -> None:
        if max_keys < 1:
            raise ValueError("InMemoryRateLimitBackend max_keys must be >= 1")
        # key -> (state, wall-clock expiry)
        self._states: dict[str, tuple[RateLimitState, float]] = {}
        self._max_keys = max_keys

    async def evaluate(self, key: str, strategy: RateLimitStrategy, now: float) -> RateLimitResult:
        # No `await` between read and write, so single-loop asyncio makes this an
        # atomic read-modify-write - two concurrent requests cannot interleave.
        entry = self._states.get(key)
        state = entry[0] if entry is not None and entry[1] > now else None
        result, new_state, ttl = strategy.evaluate(state, now)
        if key not in self._states and len(self._states) >= self._max_keys:
            self._evict(now)
        self._states[key] = (new_state, now + ttl)
        return result

    def _evict(self, now: float) -> None:
        # Drop expired keys first; if still full, drop the oldest by insertion
        # order - a memory bound without per-request LRU bookkeeping.
        expired = [k for k, (_, expires_at) in self._states.items() if expires_at <= now]
        for k in expired:
            del self._states[k]
        if len(self._states) >= self._max_keys:
            del self._states[next(iter(self._states))]
