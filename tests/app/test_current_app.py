"""current_app proxy tests (Q3)."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce, current_app
from veloce.testclient import TestClient


def test_current_app_unbound_outside_request_raises():
    with pytest.raises(RuntimeError, match="application context"):
        _ = current_app.config


def test_current_app_unbound_is_falsy():
    """The proxy supports truthiness so `if current_app:` works.

    The assertion here used to be `bool(x) is False or bool(x) is True`, which
    holds for any object whose `__bool__` returns a bool - it proved only that
    the call did not raise, never that an unbound proxy is falsy. The binding
    is a contextvar, so an unbound context is the default and can be asserted
    directly.
    """
    assert bool(current_app) is False


def test_current_app_unbound_takes_the_else_branch():
    """The behaviour the truthiness exists for, rather than the value it returns."""
    taken = "then" if current_app else "else"
    assert taken == "else"


def test_current_app_bound_is_truthy():
    """The negative: a proxy that was falsy always would pass the test above."""
    app = Veloce(openapi_url=None)
    with app.app_context():
        assert bool(current_app) is True
        assert ("then" if current_app else "else") == "then"


def test_the_binding_does_not_outlive_the_context():
    app = Veloce(openapi_url=None)
    with app.app_context():
        pass
    assert bool(current_app) is False


async def test_current_app_bound_inside_handler():
    app = Veloce(debug=True, openapi_url=None)
    app.config["MY_KEY"] = "value-from-config"

    seen: dict = {}

    @app.get("/")
    async def index():
        # current_app resolves to the app instance handling this request.
        seen["app_title"] = current_app.title
        seen["my_key"] = current_app.config["MY_KEY"]
        return {"ok": True}

    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert resp.status_code == 200
    assert seen["app_title"] == app.title
    assert seen["my_key"] == "value-from-config"


def test_two_apps_bind_independently():
    """Each app's request binds its own context — interleaved usage must
    see the right app each time."""
    app_a = Veloce(title="A", openapi_url=None, debug=True)
    app_b = Veloce(title="B", openapi_url=None, debug=True)

    @app_a.get("/who")
    async def a_who():
        return {"title": current_app.title}

    @app_b.get("/who")
    async def b_who():
        return {"title": current_app.title}

    assert TestClient(app_a).get("/who").json() == {"title": "A"}
    assert TestClient(app_b).get("/who").json() == {"title": "B"}


def test_current_app_repr():
    """Repr is informative whether bound or not."""
    # Outside a request — unbound.
    r = repr(current_app)
    assert "current_app" in r


def test_current_app_resolves_through_a_testclient_request():
    """End-to-end: the proxy resolves during ASGI dispatch, not just directly."""
    app = Veloce(openapi_url=None)
    app.config["SENTINEL"] = "I-resolved"
    observed = {}

    @app.get("/cfg")
    async def cfg_route(request: Request):
        observed["sentinel"] = current_app.config.get("SENTINEL")
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/cfg")
    assert observed["sentinel"] == "I-resolved"
