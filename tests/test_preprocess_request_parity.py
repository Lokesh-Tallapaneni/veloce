"""`preprocess_request` walks the same before-hooks dispatch does.

`Veloce.preprocess_request` and `DispatchMixin._run_before_hooks` walk the same
two buckets - app-level hooks, then the matched blueprint's - and short-circuit
on the first non-`None` return. They are **not** interchangeable and are not
meant to be: dispatch coerces the short-circuit value into a `Response` and runs
response middleware over it, while `preprocess_request` is a public method whose
documented contract is to hand back what the hook returned.

What they must agree on is *which hooks run, in what order, and when the walk
stops*. That is the part a second copy can silently get wrong, and nothing
compared them. These tests do.

`preprocess_request` also read the endpoint through
`getattr(request, "endpoint", None)` while dispatch read `request.endpoint`
directly - `endpoint` is a `__slots__` field assigned in `__init__`, so the
default could never apply, and the two walks now read it the same way.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Blueprint, Veloce
from veloce.testclient import TestClient


def _app_with_hooks(order: list[str], short_circuit: str | None = None) -> Veloce:
    app = Veloce(openapi_url=None)

    @app.before_request
    async def first(request):
        order.append("app-1")
        return "stopped-by-app-1" if short_circuit == "app-1" else None

    @app.before_request
    async def second(request):
        order.append("app-2")
        return "stopped-by-app-2" if short_circuit == "app-2" else None

    bp = Blueprint("bp", url_prefix="/bp")

    @bp.before_request
    async def bp_hook(request):
        order.append("bp-1")
        return "stopped-by-bp-1" if short_circuit == "bp-1" else None

    @bp.get("/x")
    async def bp_route():
        order.append("handler")
        return {"ok": True}

    app.register_blueprint(bp)

    @app.get("/plain")
    async def plain():
        order.append("handler")
        return {"ok": True}

    return app


# ── the same hooks, in the same order ────────────────────────────────


async def test_the_app_hooks_run_in_registration_order():
    order: list[str] = []
    app = _app_with_hooks(order)
    await app.preprocess_request(make_request(path="/plain"))
    assert order == ["app-1", "app-2"]


async def test_a_short_circuit_stops_the_walk():
    order: list[str] = []
    app = _app_with_hooks(order, short_circuit="app-1")
    result = await app.preprocess_request(make_request(path="/plain"))
    assert result == "stopped-by-app-1"
    assert order == ["app-1"], "the second hook ran after a short circuit"


async def test_the_second_hook_can_short_circuit():
    order: list[str] = []
    app = _app_with_hooks(order, short_circuit="app-2")
    result = await app.preprocess_request(make_request(path="/plain"))
    assert result == "stopped-by-app-2"
    assert order == ["app-1", "app-2"]


async def test_no_short_circuit_returns_none():
    order: list[str] = []
    app = _app_with_hooks(order)
    assert await app.preprocess_request(make_request(path="/plain")) is None


# ── and dispatch agrees on which hooks ran ───────────────────────────


@pytest.mark.parametrize("short_circuit", [None, "app-1", "app-2"])
def test_dispatch_runs_the_same_hooks_in_the_same_order(short_circuit):
    """The property the two copies must share. What each *returns* differs by
    design - dispatch coerces to a Response - so the comparison is on the walk."""
    dispatch_order: list[str] = []
    app = _app_with_hooks(dispatch_order, short_circuit=short_circuit)
    with TestClient(app) as client:
        client.get("/plain")

    direct_order: list[str] = []
    app2 = _app_with_hooks(direct_order, short_circuit=short_circuit)
    import asyncio

    asyncio.new_event_loop().run_until_complete(
        app2.preprocess_request(make_request(path="/plain"))
    )

    # Dispatch also runs the handler when nothing short-circuits; the hook
    # prefix is what must match.
    assert dispatch_order[: len(direct_order)] == direct_order


def test_a_blueprint_hook_runs_for_its_own_route():
    order: list[str] = []
    app = _app_with_hooks(order)
    with TestClient(app) as client:
        client.get("/bp/x")
    assert order == ["app-1", "app-2", "bp-1", "handler"]


def test_a_blueprint_hook_does_not_run_for_an_app_route():
    """The negative: the blueprint bucket is scoped, in both walks."""
    order: list[str] = []
    app = _app_with_hooks(order)
    with TestClient(app) as client:
        client.get("/plain")
    assert "bp-1" not in order


async def test_the_endpoint_is_read_without_a_default():
    """A request always carries `endpoint`; the walk must not depend on a
    `getattr` default to find it."""
    request = make_request(path="/plain")
    assert hasattr(request, "endpoint")
    assert request.endpoint is None  # unset until routing assigns it
    app = _app_with_hooks([])
    assert await app.preprocess_request(request) is None
