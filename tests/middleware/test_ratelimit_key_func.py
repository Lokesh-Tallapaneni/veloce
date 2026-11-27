"""`RateLimitMiddleware(key_func=...)` — naming the caller a limit applies to.

The bucket key was derived internally and only internally: the transport peer,
then the right-most `X-Forwarded-For` hop, then a `User-Agent` digest, then a
fresh `uuid4()` per request. A caller arriving with none of the first three -
an MCP tool call over stdio, any door with no client address - reached the UUID
and got a private bucket per request, so a registered limiter enforced nothing
while the stack reported it as present.

`key_func` lets the application name the caller instead, because what identifies
one is a property of the deployment rather than of the framework: an API key, a
tenant id, a subject claim. With none supplied the derivation is unchanged, so
no HTTP application's keying moves; the default now consults the authenticated
principal on an MCP call before the UUID, that door's only identity.

The negatives matter as much as the positives here: a key that collapsed
distinct callers into one bucket would let any one of them exhaust the limit for
all the others.
"""

from __future__ import annotations

import functools

import pytest

from tests._asgi_drive import drive, status_of
from tests.conftest import make_request
from veloce import Principal, RateLimitMiddleware, Request, TokenBucket, Veloce, set_principal


def _app(mw: RateLimitMiddleware) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(mw)

    @app.get("/")
    async def index():
        return {"ok": True}

    return app


def _request(*, headers: dict[str, str] | None = None, mcp: bool = False) -> Request:
    """A bare request, optionally marked as a replayed MCP call."""
    request = make_request(headers=headers)
    if mcp:
        request.state["_mcp"] = True
    return request


# ── a supplied key_func decides the bucket ───────────────────────────


async def test_one_key_for_two_client_hosts_shares_one_bucket():
    """The positive: the key_func's answer is the bucket, not the peer address."""
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=lambda _r: "tenant-7")
    app = _app(mw)
    first = await drive(app, client=("10.0.0.1", 1))
    second = await drive(app, client=("10.0.0.2", 1))
    assert status_of(first) == 200
    assert status_of(second) == 429


async def test_two_keys_do_not_share_a_bucket():
    """The negative: a limiter that merged callers would be a DoS of its own."""
    mw = RateLimitMiddleware(
        max_requests=1,
        window_seconds=60,
        key_func=lambda request: request.headers.get("x-api-key", ""),
    )
    app = _app(mw)
    first = await drive(app, headers=[(b"x-api-key", b"key-a")], client=("10.0.0.1", 1))
    second = await drive(app, headers=[(b"x-api-key", b"key-b")], client=("10.0.0.1", 1))
    assert status_of(first) == 200
    assert status_of(second) == 200


async def test_the_key_func_names_the_bucket_it_returned():
    """Namespaced: see the collision tests at the end of this module."""
    mw = RateLimitMiddleware(max_requests=10, window_seconds=60, key_func=lambda _r: "tenant-7")
    await drive(_app(mw), client=("10.0.0.1", 1))
    assert list(mw._log._buckets) == ["key:tenant-7"]


async def test_a_key_func_reaches_the_strategy_path_too():
    """Both constructors key through the same resolved function."""
    mw = RateLimitMiddleware(strategy=TokenBucket(rate=1, per=60), key_func=lambda _r: "tenant-7")
    app = _app(mw)
    first = await drive(app, client=("10.0.0.1", 1))
    second = await drive(app, client=("10.0.0.2", 1))
    assert status_of(first) == 200
    assert status_of(second) == 429


def test_a_non_callable_key_func_is_refused():
    with pytest.raises(TypeError):
        RateLimitMiddleware(key_func="tenant")  # type: ignore[arg-type]


# ── the reproduced defect: no peer, no proxy chain, no User-Agent ────


async def test_a_caller_with_no_address_or_user_agent_is_limited_by_its_key():
    """The defect. Every internal signal is absent; the key_func is all there is."""
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=lambda _r: "agent-1")
    app = _app(mw)
    first = await drive(app, client=None, headers=[])
    second = await drive(app, client=None, headers=[])
    assert status_of(first) == 200
    assert status_of(second) == 429


async def test_without_a_key_func_such_a_caller_is_still_not_limited():
    """The preserved HTTP behaviour: fail open per caller, never shared.

    With nothing to key on, each request gets its own bucket. That is the
    documented failure mode - and the reason `key_func` has to exist.
    """
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60)
    app = _app(mw)
    for _ in range(3):
        assert status_of(await drive(app, client=None, headers=[])) == 200


# ── the default chain is unchanged ───────────────────────────────────


async def test_the_default_still_keys_on_the_client_host():
    mw = RateLimitMiddleware(max_requests=10, window_seconds=60)
    await drive(_app(mw), client=("10.0.0.1", 1))
    assert list(mw._log._buckets) == ["host:10.0.0.1"]


async def test_the_default_still_separates_two_client_hosts():
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60)
    app = _app(mw)
    first = await drive(app, client=("10.0.0.1", 1))
    second = await drive(app, client=("10.0.0.2", 1))
    assert status_of(first) == 200
    assert status_of(second) == 200
    assert sorted(mw._log._buckets) == ["host:10.0.0.1", "host:10.0.0.2"]


async def test_the_default_still_limits_one_client_host():
    mw = RateLimitMiddleware(max_requests=2, window_seconds=60)
    app = _app(mw)
    statuses = [status_of(await drive(app, client=("10.0.0.9", 1))) for _ in range(4)]
    assert statuses == [200, 200, 429, 429]


async def test_the_default_still_prefers_the_rightmost_forwarded_hop():
    mw = RateLimitMiddleware(max_requests=10, window_seconds=60)
    await drive(
        _app(mw),
        client=None,
        headers=[(b"x-forwarded-for", b"203.0.113.1, 198.51.100.7")],
    )
    assert list(mw._log._buckets) == ["xff:198.51.100.7"]


# ── the MCP default: the authenticated principal ─────────────────────
#
# `conftest`'s autouse `_reset_principal` keeps the contextvar from leaking
# between these, so none of them has to put it back.


def test_an_authenticated_mcp_call_keys_on_its_principal():
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60)
    set_principal(Principal(subject="agent-1"))
    assert mw._bucket_key(_request(mcp=True)) == "sub:agent-1"


def test_two_mcp_subjects_do_not_share_a_bucket():
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60)
    set_principal(Principal(subject="agent-1"))
    first = mw._bucket_key(_request(mcp=True))
    set_principal(Principal(subject="agent-2"))
    second = mw._bucket_key(_request(mcp=True))
    assert first != second


def test_an_anonymous_mcp_principal_does_not_collapse_callers():
    """A subject-less principal is skipped: one shared bucket is the DoS."""
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60)
    set_principal(Principal())
    first = mw._bucket_key(_request(mcp=True))
    second = mw._bucket_key(_request(mcp=True))
    assert first.startswith("scope:")
    assert first != second


def test_an_unauthenticated_mcp_call_keeps_the_per_request_fallback():
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60)
    assert mw._bucket_key(_request(mcp=True)).startswith("scope:")


def test_a_principal_does_not_rekey_an_http_request():
    """The branch is MCP-only, so an HTTP caller's key cannot move."""
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60)
    set_principal(Principal(subject="agent-1"))
    assert mw._bucket_key(_request()).startswith("scope:")
    assert mw._bucket_key(_request(headers={"user-agent": "curl"})).startswith("ua:")


def test_a_key_func_wins_over_the_mcp_principal():
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=lambda _r: "explicit")
    set_principal(Principal(subject="agent-1"))
    assert mw._key(_request(mcp=True)) == "explicit"


# ── an async key_func ────────────────────────────────────────────────
#
# The identity a deployment keys on often has to be fetched - a token
# introspected, a tenant looked up - so the function naming the caller is as
# likely to be `async def` as not.


async def test_an_async_key_func_limits_the_caller_it_named():
    """The defect: an un-awaited call returns a fresh coroutine per request.

    Each one hashed to its own bucket, so a registered limiter admitted every
    request while the stack reported it as present.
    """

    async def key(_request: Request) -> str:
        return "tenant-7"

    mw = RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=key)
    app = _app(mw)
    assert status_of(await drive(app, client=("10.0.0.1", 1))) == 200
    assert status_of(await drive(app, client=("10.0.0.2", 1))) == 429


async def test_an_async_key_func_names_the_bucket_it_returned():
    async def key(_request: Request) -> str:
        return "tenant-7"

    mw = RateLimitMiddleware(max_requests=10, window_seconds=60, key_func=key)
    await drive(_app(mw), client=("10.0.0.1", 1))
    assert list(mw._log._buckets) == ["key:tenant-7"]


async def test_two_async_keys_do_not_share_a_bucket():
    """The negative: awaiting must not collapse distinct callers into one."""

    async def key(request: Request) -> str:
        return request.headers.get("x-api-key", "")

    mw = RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=key)
    app = _app(mw)
    first = await drive(app, headers=[(b"x-api-key", b"key-a")], client=("10.0.0.1", 1))
    second = await drive(app, headers=[(b"x-api-key", b"key-b")], client=("10.0.0.1", 1))
    assert status_of(first) == 200
    assert status_of(second) == 200


async def test_an_async_key_func_reaches_the_strategy_path_too():
    async def key(_request: Request) -> str:
        return "tenant-7"

    mw = RateLimitMiddleware(strategy=TokenBucket(rate=1, per=60), key_func=key)
    app = _app(mw)
    assert status_of(await drive(app, client=("10.0.0.1", 1))) == 200
    assert status_of(await drive(app, client=("10.0.0.2", 1))) == 429


async def test_an_async_key_func_reaches_the_sliding_log_path_too():
    """The third call site: the sliding-log branch keys through the same function."""

    async def key(_request: Request) -> str:
        return "tenant-7"

    mw = RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=key)
    app = _app(mw)
    assert status_of(await drive(app, client=("10.0.0.1", 1))) == 200
    assert status_of(await drive(app, client=("10.0.0.2", 1))) == 429


# ── a key_func that cannot name this caller ──────────────────────────
#
# Keying on an identity that is absent for some callers is the natural shape -
# `lambda r: r.state.user_id` names a signed-in caller and nothing else. The
# answer for the rest has to be a bucket of their own, not one shared bucket
# that the first anonymous caller can exhaust for all of them.


async def test_a_key_func_that_cannot_name_the_caller_falls_back_to_the_derived_key():
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=lambda _r: None)
    app = _app(mw)
    first = await drive(app, client=("10.0.0.1", 1))
    second = await drive(app, client=("10.0.0.2", 1))
    assert status_of(first) == 200
    assert status_of(second) == 200


async def test_an_unnamed_caller_is_still_limited_against_its_own_bucket():
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=lambda _r: None)
    app = _app(mw)
    assert status_of(await drive(app, client=("10.0.0.1", 1))) == 200
    assert status_of(await drive(app, client=("10.0.0.1", 1))) == 429


async def test_an_unnamed_caller_keys_on_the_documented_default():
    mw = RateLimitMiddleware(max_requests=10, window_seconds=60, key_func=lambda _r: None)
    await drive(_app(mw), client=("10.0.0.1", 1))
    assert list(mw._log._buckets) == ["host:10.0.0.1"]


# ── a namer that only looks synchronous ──────────────────────────────


async def test_a_lambda_wrapping_an_async_namer_still_limits():
    """Awaitability is a property of the value, not of how the namer looks.

    `key_func=lambda r: tenant(r)` inspects as a plain function and returns a
    coroutine, so deciding once at construction missed it and every request got
    a fresh object - and its own bucket.
    """

    async def tenant(_request: Request) -> str:
        return "tenant-7"

    mw = RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=lambda r: tenant(r))
    app = _app(mw)
    assert status_of(await drive(app, client=("10.0.0.1", 1))) == 200
    assert status_of(await drive(app, client=("10.0.0.2", 1))) == 429


async def test_a_partial_wrapping_an_async_namer_still_limits():
    """The same shape reached through `functools.partial`."""

    async def tenant(_request: Request, *, tag: str) -> str:
        return tag

    mw = RateLimitMiddleware(
        max_requests=1,
        window_seconds=60,
        key_func=functools.partial(tenant, tag="tenant-7"),
    )
    app = _app(mw)
    assert status_of(await drive(app, client=("10.0.0.1", 1))) == 200
    assert status_of(await drive(app, client=("10.0.0.2", 1))) == 429


# ── a namer cannot reach the derivation's buckets ────────────────────
#
# A namer returns text the caller can influence - a header, a query value - and
# the derived keys are plain strings (`host:1.2.3.4`, `ua:<digest>`). Without a
# namespace of its own, a caller sending `x-api-key: host:1.2.3.4` spent the
# budget belonging to that peer address: the next honest request from it was
# refused on its first call.


async def test_a_namer_echoing_a_derived_key_cannot_spend_its_budget():
    mw = RateLimitMiddleware(
        max_requests=1,
        window_seconds=60,
        key_func=lambda request: request.headers.get("x-api-key") or None,
    )
    app = _app(mw)
    spender = await drive(app, headers=[(b"x-api-key", b"host:10.0.0.1")], client=("10.0.0.9", 1))
    victim = await drive(app, client=("10.0.0.1", 1))
    assert status_of(spender) == 200
    # The victim's first request, which must not already be spent.
    assert status_of(victim) == 200


async def test_an_unnamed_caller_still_lands_in_the_derived_namespace():
    """The other half: a declined name must not be prefixed into a new bucket."""
    mw = RateLimitMiddleware(max_requests=10, window_seconds=60, key_func=lambda _r: None)
    await drive(_app(mw), client=("10.0.0.1", 1))
    assert list(mw._log._buckets) == ["host:10.0.0.1"]


# ── a namer that does not return the `str` it promises ───────────────
#
# The contract is `str`. Concatenating the answer made any other type a 500 on
# every request the app served, which is the wrong price for a mistake in one
# lambda - but the quiet alternative is worse, so it is said out loud once.


async def test_a_non_string_key_still_limits():
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=lambda _r: 7)
    app = _app(mw)
    assert status_of(await drive(app, client=("10.0.0.1", 1))) == 200
    assert status_of(await drive(app, client=("10.0.0.2", 1))) == 429


async def test_a_non_string_key_is_reported_once(caplog):
    mw = RateLimitMiddleware(max_requests=10, window_seconds=60, key_func=lambda _r: 7)
    app = _app(mw)
    with caplog.at_level("WARNING", logger="veloce.middleware.security"):
        for _ in range(3):
            await drive(app, client=("10.0.0.1", 1))
    warnings = [r for r in caplog.records if "key_func returned" in r.getMessage()]
    assert len(warnings) == 1
    assert "int" in warnings[0].getMessage()


async def test_a_string_key_is_not_reported(caplog):
    """The negative: the documented shape must not warn."""
    mw = RateLimitMiddleware(max_requests=10, window_seconds=60, key_func=lambda _r: "tenant-7")
    app = _app(mw)
    with caplog.at_level("WARNING", logger="veloce.middleware.security"):
        await drive(app, client=("10.0.0.1", 1))
    assert [r for r in caplog.records if "key_func returned" in r.getMessage()] == []


async def test_empty_bytes_name_nobody_either():
    """`b""` is as empty as `""`, and the two are not equal."""
    mw = RateLimitMiddleware(max_requests=1, window_seconds=60, key_func=lambda _r: b"")
    app = _app(mw)
    assert status_of(await drive(app, client=("10.0.0.1", 1))) == 200
    assert status_of(await drive(app, client=("10.0.0.2", 1))) == 200
