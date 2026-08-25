"""The Lua and Python forms of a strategy decide identically.

`RedisRateLimitBackend` runs a built-in strategy as a Lua script — one round
trip, executed atomically by the server. The previous path was a
`WATCH`/`MULTI` loop that re-read and retried on contention and, past its retry
budget, **gave up on atomicity**:

    _logger.warning("rate-limit WATCH contention on %r; using non-atomic fallback")

That mattered more than it looks. A rate limiter's hot key is contended *by
definition* — many requests for one client is the thing being limited — so the
degraded path was reachable under exactly the load the limiter exists for, and
there it admits requests over the limit. A Lua script has no `WATCH` to lose and
no fallback.

**The cost is a second implementation of each built-in algorithm**, which is the
duplication class this codebase otherwise removes. It is accepted here for the
atomicity, and made safe by this file: every strategy runs both forms over the
same inputs and the results must match, step for step. A drift between them fails
here rather than in production.

A strategy defined outside Veloce has no `lua_script` and keeps the `WATCH` path,
so the public `RateLimitStrategy` contract is unchanged — that is why the Lua form
is opt-in rather than required.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fakeredis")
pytest.importorskip("lupa", reason="fakeredis needs lupa to execute Lua")

from fakeredis.aioredis import FakeRedis  # noqa: E402

from veloce import FixedWindow, SlidingWindow, TokenBucket  # noqa: E402
from veloce.contrib.redis import RedisRateLimitBackend  # noqa: E402
from veloce.ratelimit import (  # noqa: E402
    InMemoryRateLimitBackend,
    RateLimitResult,
    RateLimitStrategy,
)

#: One instance of each built-in, at settings that exercise allow *and* refuse
#: within a short request sequence.
STRATEGIES = [
    ("FixedWindow", lambda: FixedWindow(limit=3, window=60)),
    ("FixedWindow tight", lambda: FixedWindow(limit=1, window=10)),
    ("SlidingWindow", lambda: SlidingWindow(limit=3, window=60)),
    ("SlidingWindow tight", lambda: SlidingWindow(limit=1, window=10)),
    ("TokenBucket", lambda: TokenBucket(rate=3, per=60)),
    ("TokenBucket burst", lambda: TokenBucket(rate=2, per=10, burst=5)),
    ("TokenBucket strict", lambda: TokenBucket(rate=5, per=1, burst=1)),
]

#: Time sequences: a burst, a spread, and one crossing a window boundary.
SEQUENCES = [
    ("burst", [1000.0 + i * 0.001 for i in range(8)]),
    ("spread", [1000.0 + i * 3.0 for i in range(8)]),
    ("boundary", [1000.0, 1005.0, 1009.9, 1010.0, 1010.1, 1020.0, 1059.9, 1060.1]),
    ("single", [1000.0]),
]


def _as_tuple(result: RateLimitResult) -> tuple:
    return (result.allowed, result.limit, result.remaining, result.retry_after, result.reset)


async def _run_lua(strategy: RateLimitStrategy, times: list[float]) -> list[tuple]:
    backend = RedisRateLimitBackend(FakeRedis())
    return [_as_tuple(await backend.evaluate("client", strategy, t)) for t in times]


async def _run_python(strategy: RateLimitStrategy, times: list[float]) -> list[tuple]:
    backend = InMemoryRateLimitBackend()
    return [_as_tuple(await backend.evaluate("client", strategy, t)) for t in times]


# ── the two forms agree, step for step ───────────────────────────────


@pytest.mark.parametrize(("seq_name", "times"), SEQUENCES, ids=[s[0] for s in SEQUENCES])
@pytest.mark.parametrize(("name", "factory"), STRATEGIES, ids=[s[0] for s in STRATEGIES])
async def test_lua_and_python_decide_identically(name, factory, seq_name, times):
    """The guard on the duplication: a drift fails here."""
    assert await _run_lua(factory(), times) == await _run_python(factory(), times)


@pytest.mark.parametrize(("name", "factory"), STRATEGIES, ids=[s[0] for s in STRATEGIES])
async def test_the_first_request_agrees(name, factory):
    """The `state is None` branch, which the two forms express differently."""
    assert await _run_lua(factory(), [1000.0]) == await _run_python(factory(), [1000.0])


@pytest.mark.parametrize(("name", "factory"), STRATEGIES, ids=[s[0] for s in STRATEGIES])
async def test_a_long_idle_gap_agrees(name, factory):
    """State expiry and refill after a gap far larger than the window."""
    times = [1000.0, 1000.1, 100_000.0, 100_000.1]
    assert await _run_lua(factory(), times) == await _run_python(factory(), times)


# ── each strategy actually refuses, so agreement is not vacuous ──────


@pytest.mark.parametrize(("name", "factory"), STRATEGIES, ids=[s[0] for s in STRATEGIES])
async def test_the_sequence_contains_a_refusal(name, factory):
    """Two implementations that always allow would agree trivially."""
    decisions = await _run_lua(factory(), [1000.0 + i * 0.001 for i in range(8)])
    assert any(not allowed for allowed, *_rest in decisions), name


@pytest.mark.parametrize(("name", "factory"), STRATEGIES, ids=[s[0] for s in STRATEGIES])
async def test_the_sequence_contains_an_allow(name, factory):
    decisions = await _run_lua(factory(), [1000.0])
    assert decisions[0][0] is True, name


# ── the Lua path is actually taken ───────────────────────────────────


@pytest.mark.parametrize(("name", "factory"), STRATEGIES, ids=[s[0] for s in STRATEGIES])
def test_every_built_in_declares_a_script(name, factory):
    strategy = factory()
    assert isinstance(strategy.lua_script, str)
    assert strategy.lua_argv(1000.0)


async def test_the_script_is_loaded_once_and_reused():
    """`EVALSHA` after the first call - one round trip, not two."""
    backend = RedisRateLimitBackend(FakeRedis())
    strategy = FixedWindow(limit=100, window=60)
    await backend.evaluate("c", strategy, 1000.0)
    assert len(backend._digests) == 1
    await backend.evaluate("c", strategy, 1000.1)
    assert len(backend._digests) == 1


async def test_a_flushed_script_is_reloaded():
    """A restart or failover answers NOSCRIPT; the backend reloads and retries."""
    client = FakeRedis()
    backend = RedisRateLimitBackend(client)
    strategy = FixedWindow(limit=100, window=60)
    first = await backend.evaluate("c", strategy, 1000.0)
    assert first.allowed is True

    await client.script_flush()
    second = await backend.evaluate("c", strategy, 1000.1)
    assert second.allowed is True
    assert second.remaining == 98


# ── a strategy from outside Veloce still works ───────────────────────


class CustomStrategy(RateLimitStrategy):
    """A user's own algorithm: pure Python, no Lua form."""

    __slots__ = ("limit",)

    def __init__(self, limit: int) -> None:
        self.limit = limit

    def evaluate(self, state, now):
        count = (state["count"] + 1) if state else 1
        allowed = count <= self.limit
        return (
            RateLimitResult(allowed, self.limit, max(0, self.limit - count), 0, 60),
            {"count": count},
            60,
        )


async def test_a_custom_strategy_has_no_script():
    assert CustomStrategy(2).lua_script is None


async def test_a_custom_strategy_still_works_on_redis():
    """It takes the `WATCH` path - the public contract is unchanged."""
    backend = RedisRateLimitBackend(FakeRedis())
    strategy = CustomStrategy(2)
    decisions = [(await backend.evaluate("c", strategy, 1000.0 + i)).allowed for i in range(4)]
    assert decisions == [True, True, False, False]


async def test_a_custom_strategy_agrees_with_the_in_memory_backend():
    assert await _run_lua(CustomStrategy(2), [1000.0, 1001.0, 1002.0]) == await _run_python(
        CustomStrategy(2), [1000.0, 1001.0, 1002.0]
    )


# ── state written by Lua is readable by the Python path ──────────────


@pytest.mark.parametrize(("name", "factory"), STRATEGIES, ids=[s[0] for s in STRATEGIES])
async def test_state_written_by_lua_has_the_shape_python_expects(name, factory):
    """A rolling upgrade runs both forms against one key; the state must interop."""
    import orjson

    client = FakeRedis()
    backend = RedisRateLimitBackend(client)
    strategy = factory()
    await backend.evaluate("c", strategy, 1000.0)
    raw = await client.get("veloce:ratelimit:c")
    state = orjson.loads(raw)
    # The Python form must accept it without raising.
    strategy.evaluate(state, 1000.5)


@pytest.mark.parametrize(("name", "factory"), STRATEGIES, ids=[s[0] for s in STRATEGIES])
async def test_lua_reads_state_python_wrote(name, factory):
    """The other direction of the same upgrade."""
    import orjson

    client = FakeRedis()
    backend = RedisRateLimitBackend(client)
    strategy = factory()
    _result, state, ttl = strategy.evaluate(None, 1000.0)
    await client.set("veloce:ratelimit:c", orjson.dumps(state), ex=ttl)
    second = await backend.evaluate("c", strategy, 1000.001)
    reference = await _run_python(factory(), [1000.0, 1000.001])
    assert _as_tuple(second) == reference[1]


# ── the key is passed as KEYS[1], so a cluster can route it ──────────


def test_the_script_uses_keys_not_a_literal():
    """Redis Cluster routes by the declared key; a literal would break there."""
    for _name, factory in STRATEGIES:
        script = factory().lua_script
        assert "KEYS[1]" in script
        assert "veloce:ratelimit" not in script
