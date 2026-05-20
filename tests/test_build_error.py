"""BuildError + app.url_build_error_handlers."""

from __future__ import annotations

import pytest

from veloce import BuildError, Veloce


def test_url_for_unknown_endpoint_raises_build_error():
    app = Veloce()
    with pytest.raises(BuildError) as info:
        app.url_for("does_not_exist")
    assert info.value.endpoint == "does_not_exist"


def test_build_error_carries_values():
    app = Veloce()

    @app.get("/item/{id:int}")
    async def item(id: int):
        return {}

    # Missing path param → BuildError with the kwargs that were supplied.
    with pytest.raises(BuildError) as info:
        app.url_for("item")  # no id=
    assert info.value.endpoint == "item"
    assert info.value.values == {}


def test_handler_recovers_with_fallback_url():
    app = Veloce()

    def fallback(error, endpoint, values):
        if endpoint == "missing":
            return f"/fallback/{endpoint}"
        return None

    app.url_build_error_handlers.append(fallback)
    assert app.url_for("missing") == "/fallback/missing"


def test_handler_returning_none_falls_through_to_next():
    app = Veloce()
    calls: list[str] = []

    def h1(error, endpoint, values):
        calls.append("h1")
        return None

    def h2(error, endpoint, values):
        calls.append("h2")
        return "/recovered"

    app.url_build_error_handlers.append(h1)
    app.url_build_error_handlers.append(h2)
    assert app.url_for("x") == "/recovered"
    assert calls == ["h1", "h2"]


def test_no_handler_recovers_raises():
    app = Veloce()
    app.url_build_error_handlers.append(lambda e, n, v: None)
    with pytest.raises(BuildError):
        app.url_for("missing")


def test_url_build_error_handlers_is_public_list():
    app = Veloce()
    # Mutable list users append to.
    assert app.url_build_error_handlers == []
    app.url_build_error_handlers.append(lambda e, n, v: "/x")
    assert len(app.url_build_error_handlers) == 1


def test_build_error_is_lookup_error_subclass():
    """Existing `except LookupError` handlers catch BuildError."""
    app = Veloce()
    with pytest.raises(LookupError):
        app.url_for("missing")


def test_successful_url_for_does_not_invoke_handlers():
    app = Veloce()

    @app.get("/x", name="x")
    async def x():
        return {}

    called: list[bool] = []
    app.url_build_error_handlers.append(lambda e, n, v: called.append(True))
    assert app.url_for("x") == "/x"
    assert called == []
