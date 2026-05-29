"""app.add_event_handler — imperative event-handler registration."""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.testclient import TestClient


def test_add_startup_handler_runs():
    app = Veloce()
    fired: list[str] = []

    async def on_start():
        fired.append("started")

    app.add_event_handler("startup", on_start)
    with TestClient(app):
        pass
    assert fired == ["started"]


def test_add_shutdown_handler_runs():
    app = Veloce()
    fired: list[str] = []

    async def on_stop():
        fired.append("stopped")

    app.add_event_handler("shutdown", on_stop)
    client = TestClient(app)
    client.close()
    assert fired == ["stopped"]


def test_add_event_handler_invalid_event_raises():
    app = Veloce()
    with pytest.raises(ValueError, match="startup.*shutdown"):
        app.add_event_handler("bogus", lambda: None)


def test_add_event_handler_appends_to_same_list_as_decorator():
    app = Veloce()

    @app.on_event("startup")
    async def decorated():
        pass

    async def imperative():
        pass

    app.add_event_handler("startup", imperative)
    assert decorated in app._on_startup
    assert imperative in app._on_startup


def test_add_event_handler_emits_deprecation_warning():
    app = Veloce()

    async def handler():
        pass

    with pytest.warns(DeprecationWarning, match="on_startup"):
        app.add_event_handler("startup", handler)
    assert handler in app._on_startup


def test_on_event_emits_deprecation_warning():
    app = Veloce()

    with pytest.warns(DeprecationWarning, match="on_startup"):

        @app.on_event("startup")
        async def handler():
            pass

    assert handler in app._on_startup


def test_add_event_handler_ordering_preserved():
    app = Veloce()
    order: list[int] = []

    async def first():
        order.append(1)

    async def second():
        order.append(2)

    app.add_event_handler("startup", first)
    app.add_event_handler("startup", second)
    with TestClient(app):
        pass
    assert order == [1, 2]
