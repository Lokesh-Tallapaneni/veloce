"""Tests for duplicate-route detection.

Registering a second handler for the same path+method is, by default, a
`DuplicateRouteError` raised at registration time - catching silent route
shadowing at startup. The policy is configurable per router with
`on_duplicate="error"|"warn"|"override"`. An idempotent re-mount (the same
handler landing again, as with `include_router` called twice) is never a
conflict.
"""

from __future__ import annotations

import logging

import pytest

from veloce import DuplicateRouteError, Router, Veloce
from veloce.testclient import TestClient


def test_default_policy_raises_on_duplicate():
    app = Veloce()

    @app.get("/x")
    async def first():
        return {"h": 1}

    with pytest.raises(DuplicateRouteError) as exc:

        @app.get("/x")
        async def second():
            return {"h": 2}

    err = exc.value
    assert err.path == "/x"
    assert err.method == "GET"
    assert "first" in err.existing
    assert "second" in err.incoming


def test_different_methods_same_path_not_a_conflict():
    app = Veloce()

    @app.get("/y")
    async def getter():
        return {"m": "get"}

    @app.post("/y")
    async def poster():
        return {"m": "post"}

    client = TestClient(app)
    assert client.get("/y").json() == {"m": "get"}
    assert client.post("/y").json() == {"m": "post"}


def test_override_policy_replaces_silently():
    app = Veloce(on_duplicate="override")

    @app.get("/z")
    async def first():
        return {"h": 1}

    @app.get("/z")
    async def second():
        return {"h": 2}

    client = TestClient(app)
    assert client.get("/z").json() == {"h": 2}


def test_warn_policy_logs_and_replaces(caplog):
    app = Veloce(on_duplicate="warn")

    @app.get("/w")
    async def first():
        return {"h": 1}

    with caplog.at_level(logging.WARNING, logger="veloce.routing.router"):

        @app.get("/w")
        async def second():
            return {"h": 2}

    assert any("Duplicate route" in r.message for r in caplog.records)
    client = TestClient(app)
    assert client.get("/w").json() == {"h": 2}


def test_invalid_policy_rejected():
    with pytest.raises(ValueError):
        Veloce(on_duplicate="bogus")
    with pytest.raises(ValueError):
        Router(on_duplicate="nope")


def test_duplicate_on_regex_route_detected():
    app = Veloce()

    @app.get("/files/{name}.{ext}")
    async def first(name: str, ext: str):
        return {"h": 1}

    with pytest.raises(DuplicateRouteError):

        @app.get("/files/{name}.{ext}")
        async def second(name: str, ext: str):
            return {"h": 2}


def test_idempotent_remount_is_not_a_conflict():
    sub = Router()

    @sub.get("/sub")
    async def handler():
        return {"ok": True}

    app = Veloce()
    app.include_router(sub)
    # Re-including the same router carries the same handler callable, which is
    # an idempotent re-mount, not a conflict.
    app.include_router(sub)

    client = TestClient(app)
    assert client.get("/sub").json() == {"ok": True}


def test_merge_conflict_detected():
    sub = Router()

    @sub.get("/c")
    async def from_sub():
        return {"src": "sub"}

    app = Veloce()

    @app.get("/c")
    async def from_app():
        return {"src": "app"}

    with pytest.raises(DuplicateRouteError):
        app.include_router(sub)


def test_merge_conflict_allowed_with_override():
    sub = Router()

    @sub.get("/c")
    async def from_sub():
        return {"src": "sub"}

    app = Veloce(on_duplicate="override")

    @app.get("/c")
    async def from_app():
        return {"src": "app"}

    app.include_router(sub)
    client = TestClient(app)
    assert client.get("/c").json() == {"src": "sub"}


def test_router_level_duplicate_raises():
    router = Router()

    @router.get("/r")
    async def first():
        return {"h": 1}

    with pytest.raises(DuplicateRouteError):

        @router.get("/r")
        async def second():
            return {"h": 2}


def test_caught_duplicate_does_not_pollute_url_for():
    # A rejected duplicate registration must leave no reverse (url_for) entry:
    # the named route is only committed after the duplicate policy passes, so a
    # caught DuplicateRouteError cannot leave url_for() resolving a route that
    # was never installed in the dispatch table.
    app = Veloce()

    @app.get("/p", name="winner")
    async def first():
        return {"h": 1}

    with pytest.raises(DuplicateRouteError):

        @app.get("/p", name="loser")
        async def second():
            return {"h": 2}

    # The rejected route's name must not be reachable via url_for. Veloce wraps
    # the unknown-endpoint ValueError in BuildError.
    from veloce.exceptions import BuildError

    with pytest.raises(BuildError):
        app.url_for("loser")
    # The committed route still reverses correctly.
    assert app.url_for("winner") == "/p"


def test_override_url_for_reflects_winning_route():
    # On the override replace path the reverse entry must reflect the route
    # that actually wins (its template), not a stale or partial one.
    app = Veloce(on_duplicate="override")

    @app.get("/a", name="ep")
    async def first():
        return {"h": 1}

    @app.get("/a", name="ep")
    async def second():
        return {"h": 2}

    assert app.url_for("ep") == "/a"
    client = TestClient(app)
    assert client.get("/a").json() == {"h": 2}


def test_override_replace_removes_old_name_reverse_entry():
    # An override replace under a DIFFERENT name must drop the replaced route's
    # reverse entry: the old route is gone from the handler table, so
    # url_for(old_name) must stop resolving while url_for(new_name) works.
    from veloce.exceptions import BuildError

    app = Veloce(on_duplicate="override")

    @app.get("/o", name="old")
    async def first():
        return {"h": 1}

    @app.get("/o", name="new")
    async def second():
        return {"h": 2}

    assert app.url_for("new") == "/o"
    with pytest.raises(BuildError):
        app.url_for("old")
    client = TestClient(app)
    assert client.get("/o").json() == {"h": 2}


def test_warn_replace_removes_old_name_reverse_entry(caplog):
    # Same as the override case but on the `warn` policy: the replace still wins,
    # so the displaced name's reverse entry must be removed.
    from veloce.exceptions import BuildError

    app = Veloce(on_duplicate="warn")

    @app.get("/o", name="old")
    async def first():
        return {"h": 1}

    with caplog.at_level(logging.WARNING, logger="veloce.routing.router"):

        @app.get("/o", name="new")
        async def second():
            return {"h": 2}

    assert app.url_for("new") == "/o"
    with pytest.raises(BuildError):
        app.url_for("old")


def test_same_callable_different_name_is_a_conflict():
    # A same-callable registration carrying different route metadata (here a
    # different name) is a real second registration, not an idempotent remount,
    # so it must go through the on_duplicate policy rather than being silently
    # exempted by handler identity alone.
    app = Veloce()

    async def shared():
        return {"ok": True}

    app.add_route("/s", shared, methods=["GET"], name="alpha")
    with pytest.raises(DuplicateRouteError):
        app.add_route("/s", shared, methods=["GET"], name="beta")


def test_same_callable_different_response_model_is_a_conflict():
    # Same callable, different response_model -> distinct behaviour, so the
    # collision must not be exempted as an idempotent remount.
    from pydantic import BaseModel

    class A(BaseModel):
        x: int

    class B(BaseModel):
        y: int

    app = Veloce()

    async def shared():
        return {"x": 1}

    app.add_route("/m", shared, methods=["GET"], response_model=A)
    with pytest.raises(DuplicateRouteError):
        app.add_route("/m", shared, methods=["GET"], response_model=B)


def test_same_callable_identical_metadata_is_idempotent():
    # The genuine idempotent remount - identical handler AND identical
    # route-defining metadata - is still exempt regardless of policy.
    app = Veloce()

    async def shared():
        return {"ok": True}

    app.add_route("/i", shared, methods=["GET"], name="same")
    # Re-registering with identical metadata must not raise.
    app.add_route("/i", shared, methods=["GET"], name="same")

    client = TestClient(app)
    assert client.get("/i").json() == {"ok": True}
