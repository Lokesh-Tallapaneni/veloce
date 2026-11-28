"""app.dependency_overrides test-time dependency swapping."""

from __future__ import annotations

import orjson

from tests.conftest import make_request
from veloce import Depends, Veloce
from veloce.testclient import TestClient


def _real_dep() -> str:
    return "real"


def test_overrides_starts_empty():
    app = Veloce(openapi_url=None)
    assert app.dependency_overrides == {}


def test_override_swaps_dependency():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(value: str = Depends(_real_dep)):
        return {"value": value}

    def _fake_dep() -> str:
        return "fake"

    app.dependency_overrides[_real_dep] = _fake_dep

    with TestClient(app) as client:
        assert client.get("/x").json() == {"value": "fake"}


def test_clearing_overrides_restores_real_dependency():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(value: str = Depends(_real_dep)):
        return {"value": value}

    app.dependency_overrides[_real_dep] = lambda: "fake"

    with TestClient(app) as client:
        assert client.get("/x").json()["value"] == "fake"

    app.dependency_overrides.clear()

    with TestClient(app) as client:
        assert client.get("/x").json()["value"] == "real"


def test_assigning_fresh_dict_replaces_overrides():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(value: str = Depends(_real_dep)):
        return {"value": value}

    app.dependency_overrides[_real_dep] = lambda: "fake"
    app.dependency_overrides = {}

    with TestClient(app) as client:
        assert client.get("/x").json()["value"] == "real"


def test_provider_method_and_property_share_storage():
    app = Veloce(openapi_url=None)
    app.dependency_overrides[_real_dep] = _real_dep
    assert app.dependency_overrides_provider() is app.dependency_overrides


async def test_an_override_replaces_the_dependency_for_a_direct_dispatch():
    app = Veloce(openapi_url=None)

    def get_db():
        return {"real": True}

    def get_mock_db():
        return {"mock": True}

    @app.get("/db")
    async def db_route(db=Depends(get_db)):
        return db

    # Without override
    resp = await app.handle_request(make_request(path="/db"))
    assert orjson.loads(resp.body)["real"] is True

    # With override
    app.dependency_overrides[get_db] = get_mock_db
    resp = await app.handle_request(make_request(path="/db"))
    assert orjson.loads(resp.body)["mock"] is True
