"""Plugin protocol — app.install() and the Plugin contract."""

from __future__ import annotations

import pytest

from veloce import Plugin, Veloce


def test_install_calls_plugin_install_with_app():
    seen = []

    class P:
        def install(self, app):
            seen.append(app)

    app = Veloce(openapi_url=None)
    p = P()
    app.install(p)
    assert seen == [app]


def test_install_returns_the_plugin():
    class P:
        def install(self, app): ...

    app = Veloce(openapi_url=None)
    p = P()
    assert app.install(p) is p


def test_named_plugin_recorded_in_extensions():
    class P:
        name = "thing"

        def install(self, app): ...

    app = Veloce(openapi_url=None)
    p = P()
    app.install(p)
    assert app.extensions["thing"] is p


def test_duplicate_name_raises_value_error():
    class P:
        name = "dup"

        def install(self, app): ...

    app = Veloce(openapi_url=None)
    app.install(P())
    with pytest.raises(ValueError, match="dup"):
        app.install(P())


def test_unnamed_plugin_is_fire_and_forget():
    class P:
        def install(self, app): ...

    app = Veloce(openapi_url=None)
    before = dict(app.extensions)
    app.install(P())
    assert app.extensions == before


def test_plugin_can_register_a_route():
    class RoutePlugin:
        def install(self, app):
            @app.get("/ping")
            async def ping():
                return {"ok": True}

    app = Veloce(openapi_url=None)
    app.install(RoutePlugin())
    client = app.test_client()
    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_plugin_prerequisite_error_propagates():
    class Needs:
        name = "needs"

        def install(self, app):
            if "base" not in app.extensions:
                raise RuntimeError("requires 'base' plugin first")

    app = Veloce(openapi_url=None)
    with pytest.raises(RuntimeError, match="requires 'base'"):
        app.install(Needs())
    assert "needs" not in app.extensions


def test_non_plugin_raises_type_error():
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError):
        app.install(lambda app: None)


def test_plugin_is_runtime_checkable():
    class P:
        def install(self, app): ...

    assert isinstance(P(), Plugin)
    assert not isinstance(object(), Plugin)
