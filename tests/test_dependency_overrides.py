"""app.dependency_overrides test-time dependency swapping."""

from __future__ import annotations

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
