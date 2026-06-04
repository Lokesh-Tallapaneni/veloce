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
