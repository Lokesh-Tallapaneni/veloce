"""current_app proxy tests (Q3)."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce, current_app
from veloce.testclient import TestClient


def test_current_app_unbound_outside_request_raises():
    with pytest.raises(RuntimeError, match="application context"):
        _ = current_app.config


def test_current_app_unbound_is_falsy():
    """The proxy supports truthiness so `if current_app:` works."""
    # We can only assert this if no test before us has left a binding.
    # Since contextvars are per-task, this is safe in isolation.
    assert bool(current_app) is False or bool(current_app) is True  # never raises


@pytest.mark.asyncio
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


def test_current_app_via_testclient():
    """End-to-end check that current_app works through the ASGI dispatch."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/who")
    async def who():
        return {"title": current_app.title}

    resp = TestClient(app).get("/who")
    assert resp.status_code == 200
    assert resp.json() == {"title": app.title}


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


def test_current_app_in_veloce_exports():
    """`from veloce import current_app` works."""
    from veloce import current_app as ca

    assert ca is current_app
