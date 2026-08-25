"""A `dependencies=` entry that is not a `Depends` is refused, not dropped.

`build_route_dep_plans` kept the entries that were `Depends` instances and
silently discarded everything else. So forgetting the wrapper — writing
`dependencies=[guard]` instead of `dependencies=[Depends(guard)]` — registered
with no error, no warning and no log line, and the guard never ran:

    Veloce(dependencies=[guard])        -> GET /secret  200 {"secret": "leaked"}
    Veloce(dependencies=[Depends(guard)]) -> GET /secret  401 {"detail": "denied"}

The source reads as protected and every route is open. That is the third silent
authorisation failure in this audit, alongside the nested-blueprint hooks and the
SSE principal, and it is the easiest of the three to write by accident.

The entry is now a `TypeError` at registration, on the line that declared the
route, naming the callable and the wrapper to put around it.
"""

from __future__ import annotations

import logging
import warnings

import pytest

from veloce import Blueprint, Depends, HTTPException, Request, Security, Veloce
from veloce.security import APIKeyHeader
from veloce.testclient import TestClient


def guard() -> None:
    raise HTTPException(status_code=401, detail="denied")


async def async_guard() -> None:
    raise HTTPException(status_code=401, detail="denied")


# ── the reported hole ────────────────────────────────────────────────


def test_a_bare_callable_at_app_level_is_refused():
    """The defect: this registered silently and left every route open."""
    with pytest.raises(TypeError, match="not a bare callable"):
        app = Veloce(openapi_url=None, dependencies=[guard])

        @app.get("/secret")
        async def secret(request: Request) -> dict:
            return {"secret": "leaked"}


def test_a_bare_callable_at_route_level_is_refused():
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="not a bare callable"):

        @app.get("/secret", dependencies=[guard])
        async def secret() -> dict:
            return {}


def test_a_bare_callable_at_router_level_is_refused():
    from veloce import Router

    router = Router(dependencies=[guard])
    with pytest.raises(TypeError, match="not a bare callable"):

        @router.get("/secret")
        async def secret() -> dict:
            return {}


def test_a_bare_callable_on_a_blueprint_is_refused():
    bp = Blueprint("bp", url_prefix="/bp", dependencies=[guard])
    with pytest.raises(TypeError, match="not a bare callable"):

        @bp.get("/secret")
        async def secret() -> dict:
            return {}


def test_an_async_bare_callable_is_refused():
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="not a bare callable"):

        @app.get("/secret", dependencies=[async_guard])
        async def secret() -> dict:
            return {}


def test_a_bare_callable_on_a_websocket_route_is_refused():
    """`websocket()` takes none, but `add_route` reaches the same planner."""
    app = Veloce(openapi_url=None)

    async def ws(websocket) -> None:
        await websocket.accept()

    with pytest.raises(TypeError, match="not a bare callable"):
        app.add_route(path="/ws", handler=ws, methods=["WEBSOCKET"], dependencies=[guard])


def test_a_depends_on_a_websocket_route_is_accepted():
    app = Veloce(openapi_url=None)

    async def ws(websocket) -> None:
        await websocket.accept()

    app.add_route(path="/ws", handler=ws, methods=["WEBSOCKET"], dependencies=[Depends(guard)])
    assert app.match("WEBSOCKET", "/ws") is not None


# ── the message is actionable ────────────────────────────────────────


def test_the_message_names_the_callable():
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="guard"):

        @app.get("/x", dependencies=[guard])
        async def x() -> dict:
            return {}


def test_the_message_shows_the_wrapper_to_use():
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match=r"Wrap it as Depends\(guard\)"):

        @app.get("/x", dependencies=[guard])
        async def x() -> dict:
            return {}


def test_a_lambda_is_named_gracefully():
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="lambda"):

        @app.get("/x", dependencies=[lambda: None])
        async def x() -> dict:
            return {}


def test_a_callable_object_without_a_name_is_still_reported():
    class Callable:
        def __call__(self) -> None:
            pass

    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="the callable"):

        @app.get("/x", dependencies=[Callable()])
        async def x() -> dict:
            return {}


@pytest.mark.parametrize("value", ["nope", 42, None, {"a": 1}, ["nested"], object()])
def test_a_non_callable_entry_is_refused(value):
    """It cannot be a dependency under any reading, wrapper or not."""
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="dependencies="):

        @app.get("/x", dependencies=[value])
        async def x() -> dict:
            return {}


def test_the_non_callable_message_says_why():
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="not callable"):

        @app.get("/x", dependencies=["nope"])
        async def x() -> dict:
            return {}


# ── what must keep working ───────────────────────────────────────────


def test_depends_still_runs_at_app_level():
    app = Veloce(openapi_url=None, dependencies=[Depends(guard)])

    @app.get("/secret")
    async def secret(request: Request) -> dict:
        return {"secret": "leaked"}

    assert TestClient(app).get("/secret").status_code == 401


def test_depends_still_runs_at_route_level():
    app = Veloce(openapi_url=None)

    @app.get("/secret", dependencies=[Depends(guard)])
    async def secret() -> dict:
        return {}

    assert TestClient(app).get("/secret").status_code == 401


def test_security_is_accepted():
    """`Security` subclasses `Depends`, so the check must not refuse it."""
    scheme = APIKeyHeader(name="X-Key", auto_error=False)
    app = Veloce(openapi_url=None)

    @app.get("/x", dependencies=[Security(scheme)])
    async def x() -> dict:
        return {"ok": True}

    assert TestClient(app).get("/x").status_code == 200


def test_a_security_with_scopes_is_accepted():
    scheme = APIKeyHeader(name="X-Key", auto_error=False)
    app = Veloce(openapi_url=None)

    @app.get("/x", dependencies=[Security(scheme, scopes=["read"])])
    async def x() -> dict:
        return {"ok": True}

    assert TestClient(app).get("/x").status_code == 200


def test_an_empty_list_is_fine():
    app = Veloce(openapi_url=None)

    @app.get("/x", dependencies=[])
    async def x() -> dict:
        return {"ok": True}

    assert TestClient(app).get("/x").status_code == 200


def test_no_dependencies_argument_is_fine():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    assert TestClient(app).get("/x").status_code == 200


def test_several_valid_entries_all_run():
    order = []

    def first() -> None:
        order.append("first")

    def second() -> None:
        order.append("second")

    app = Veloce(openapi_url=None)

    @app.get("/x", dependencies=[Depends(first), Depends(second)])
    async def x() -> dict:
        return {"ok": True}

    TestClient(app).get("/x")
    assert order == ["first", "second"]


def test_app_and_route_dependencies_compose():
    order = []

    def outer() -> None:
        order.append("app")

    def inner() -> None:
        order.append("route")

    app = Veloce(openapi_url=None, dependencies=[Depends(outer)])

    @app.get("/x", dependencies=[Depends(inner)])
    async def x() -> dict:
        return {"ok": True}

    TestClient(app).get("/x")
    assert order == ["app", "route"]


def test_a_blueprint_dependency_still_runs():
    bp = Blueprint("bp", url_prefix="/bp", dependencies=[Depends(guard)])

    @bp.get("/secret")
    async def secret() -> dict:
        return {}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)
    assert TestClient(app).get("/bp/secret").status_code == 401


# ── the mistake is loud, not merely observable ───────────────────────


def test_nothing_was_logged_or_warned_before_and_now_it_raises(caplog):
    """The old failure emitted nothing at any level; that was the whole problem."""
    app = Veloce(openapi_url=None)
    with (
        warnings.catch_warnings(record=True) as caught,
        caplog.at_level(logging.DEBUG),
        pytest.raises(TypeError),
    ):
        warnings.simplefilter("always")

        @app.get("/x", dependencies=[guard])
        async def x() -> dict:
            return {}

    # A raise, not a warning that a caller could miss.
    assert [w for w in caught if issubclass(w.category, UserWarning)] == []


def test_the_route_is_not_registered_when_the_entry_is_refused():
    """A half-registered route would be worse than either outcome."""
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError):

        @app.get("/secret", dependencies=[guard])
        async def secret() -> dict:
            return {}

    assert TestClient(app).get("/secret").status_code == 404
