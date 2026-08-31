"""A compiled `yield` dependency tears down exactly as the interpreted one does.

`compile_graph_resolver` used to bail on any `yield`-teardown dependency, so a
route using one ran the interpreted resolver. The only resolve-time work is
starting the generator, taking the first yielded value, and pushing
`(kind, generator)` onto the teardown stack `run_teardowns` drains in reverse -
so the stack is passed into the compiled body and everything after the `yield`
is unchanged.

Teardown ordering and exception delivery are what a resource-releasing
dependency relies on, so both are compared against the interpreter rather than
merely asserted.
"""

from __future__ import annotations

import pytest

from veloce import Depends, Veloce
from veloce.testclient import TestClient


def _unused() -> None:  # pragma: no cover - only ever an override map key
    return None


def _build(app: Veloce, log: list[str]) -> None:
    def sync_gen():
        log.append("sync:setup")
        try:
            yield "sync-value"
        finally:
            log.append("sync:teardown")

    async def async_gen():
        log.append("async:setup")
        try:
            yield "async-value"
        finally:
            log.append("async:teardown")

    def outer_gen(inner: str = Depends(sync_gen)):
        log.append("outer:setup")
        try:
            yield f"outer({inner})"
        finally:
            log.append("outer:teardown")

    async def catcher():
        log.append("catcher:setup")
        try:
            yield "caught"
        except Exception as exc:  # noqa: BLE001 - the point is observing it
            log.append(f"catcher:saw:{type(exc).__name__}")
            raise
        finally:
            log.append("catcher:teardown")

    @app.get("/sync")
    async def sync_route(v: str = Depends(sync_gen)) -> dict:
        log.append("handler")
        return {"v": v}

    @app.get("/async")
    async def async_route(v: str = Depends(async_gen)) -> dict:
        log.append("handler")
        return {"v": v}

    @app.get("/both")
    async def both(a: str = Depends(sync_gen), b: str = Depends(async_gen)) -> dict:
        log.append("handler")
        return {"a": a, "b": b}

    @app.get("/nested")
    async def nested(v: str = Depends(outer_gen)) -> dict:
        log.append("handler")
        return {"v": v}

    @app.get("/raises")
    async def raises(v: str = Depends(catcher)) -> dict:
        raise RuntimeError("boom")

    @app.get("/mixed")
    async def mixed(g: str = Depends(sync_gen), p: str = Depends(_plain)) -> dict:
        return {"g": g, "p": p}


def _plain() -> str:
    return "plain"


def _apps() -> tuple[tuple[Veloce, list[str]], tuple[Veloce, list[str]]]:
    """(compiled, interpreted) - the second forced onto the interpreter."""
    compiled_log: list[str] = []
    compiled = Veloce(openapi_url=None)
    _build(compiled, compiled_log)

    interpreted_log: list[str] = []
    interpreted = Veloce(openapi_url=None)
    _build(interpreted, interpreted_log)
    interpreted.dependency_overrides[_unused] = _unused

    return (compiled, compiled_log), (interpreted, interpreted_log)


ROUTES = ["/sync", "/async", "/both", "/nested", "/mixed"]


# ── the compiled path is genuinely under test ────────────────────────


def test_a_yield_dependency_route_gets_a_compiled_resolver():
    """The bail this change removed. Without it every case below is vacuous."""
    app = Veloce(openapi_url=None)
    _build(app, [])
    TestClient(app).get("/sync")
    plan = next(i.handler_plan for _m, p, i in app.iter_routes() if p == "/sync")
    assert plan is not None
    assert plan.compiled_graph_resolver is not None
    assert getattr(plan.compiled_graph_resolver, "__name__", "") == "_resolver"


# ── parity ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ROUTES)
def test_both_resolvers_run_the_same_teardowns_in_the_same_order(path: str):
    (compiled, clog), (interpreted, ilog) = _apps()
    a = TestClient(compiled).get(path)
    b = TestClient(interpreted).get(path)
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    assert clog == ilog, f"{path}: {clog} != {ilog}"


def test_both_resolvers_deliver_the_request_exception_to_the_generator():
    (compiled, clog), (interpreted, ilog) = _apps()
    a = TestClient(compiled).get("/raises")
    b = TestClient(interpreted).get("/raises")
    assert a.status_code == b.status_code == 500
    assert clog == ilog
    assert "catcher:saw:RuntimeError" in clog


# ── the contract itself ──────────────────────────────────────────────


def test_setup_runs_before_the_handler_and_teardown_after():
    (compiled, log), _ = _apps()
    TestClient(compiled).get("/sync")
    assert log == ["sync:setup", "handler", "sync:teardown"]


def test_teardowns_drain_in_reverse_registration_order():
    (compiled, log), _ = _apps()
    TestClient(compiled).get("/both")
    assert log == [
        "sync:setup",
        "async:setup",
        "handler",
        "async:teardown",
        "sync:teardown",
    ]


def test_a_nested_yield_dependency_tears_down_inside_out():
    (compiled, log), _ = _apps()
    TestClient(compiled).get("/nested")
    assert log == [
        "sync:setup",
        "outer:setup",
        "handler",
        "outer:teardown",
        "sync:teardown",
    ]


def test_the_yielded_value_reaches_the_handler():
    (compiled, _log), _ = _apps()
    assert TestClient(compiled).get("/nested").json() == {"v": "outer(sync-value)"}


def _empty_gen():
    if False:  # pragma: no cover - makes it a generator without yielding
        yield None


def _never_yields_app(interpreted: bool) -> tuple[Veloce, list[str]]:
    seen: list[str] = []
    app = Veloce(openapi_url=None)

    @app.get("/e")
    async def e(v=Depends(_empty_gen)) -> dict:
        return {"v": v}

    @app.errorhandler(RuntimeError)
    async def on_error(request, exc):
        seen.append(str(exc))
        return {"caught": True}

    if interpreted:
        app.dependency_overrides[_unused] = _unused
    return app, seen


def test_a_generator_that_never_yields_reports_the_same_error_either_way():
    """The dispatcher turns it into a 500, so the message is compared, not the raise."""
    compiled, compiled_seen = _never_yields_app(interpreted=False)
    interpreted, interpreted_seen = _never_yields_app(interpreted=True)
    TestClient(compiled).get("/e")
    TestClient(interpreted).get("/e")
    assert compiled_seen, "the compiled path did not raise for a non-yielding dependency"
    assert compiled_seen == interpreted_seen
    assert "returned without yielding a value" in compiled_seen[0]


def _failing_setup_app(interpreted: bool) -> tuple[Veloce, list[str]]:
    """A dependency that raises before it ever yields."""
    log: list[str] = []
    app = Veloce(openapi_url=None)

    def broken():
        log.append("setup")
        raise ValueError("setup failed")
        yield "never"  # pragma: no cover - makes it a generator

    @app.get("/broken")
    async def broken_route(v=Depends(broken)) -> dict:
        return {"v": v}

    @app.errorhandler(Exception)
    async def on_error(request, exc):
        log.append(f"handled:{type(exc).__name__}")
        return {"caught": True}

    if interpreted:
        app.dependency_overrides[_unused] = _unused
    return app, log


def test_a_dependency_that_fails_before_yielding_is_never_torn_down():
    """The generator is registered only once the first yield has succeeded.

    Registering it beforehand looks equivalent and is not: a setup that raises
    would then be thrown into during teardown, turning a clean failure into a
    second, confusing one that re-raises the request exception.
    """
    compiled, compiled_log = _failing_setup_app(interpreted=False)
    interpreted, interpreted_log = _failing_setup_app(interpreted=True)
    TestClient(compiled).get("/broken")
    TestClient(interpreted).get("/broken")
    assert compiled_log == interpreted_log, f"{compiled_log} != {interpreted_log}"
    assert compiled_log == ["setup", "handled:ValueError"], compiled_log


def test_teardown_still_runs_when_the_handler_raises():
    (compiled, log), _ = _apps()
    TestClient(compiled).get("/raises")
    assert "catcher:teardown" in log


def test_repeated_requests_do_not_accumulate_teardowns():
    """A generator left on the stack would tear down twice on the next request."""
    (compiled, log), _ = _apps()
    client = TestClient(compiled)
    client.get("/sync")
    log.clear()
    client.get("/sync")
    assert log == ["sync:setup", "handler", "sync:teardown"]
