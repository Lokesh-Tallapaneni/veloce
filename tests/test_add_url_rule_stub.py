"""add_url_rule endpoint-only stubs (R10)."""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.testclient import TestClient


def test_endpoint_only_stub_resolves_in_url_for():
    app = Veloce()
    app.add_url_rule("/reports/{year:int}", endpoint="report")
    # url_for works even though no handler is attached.
    assert app.url_for("report", year=2026) == "/reports/2026"


def test_endpoint_only_stub_requires_endpoint_name():
    app = Veloce()
    with pytest.raises(ValueError, match="endpoint"):
        app.add_url_rule("/x")


def test_stub_then_attach_handler_with_endpoint_decorator():
    app = Veloce(debug=True)
    app.add_url_rule("/page", endpoint="page")

    @app.endpoint("page")
    async def page_view():
        return {"page": "live"}

    with TestClient(app) as client:
        resp = client.get("/page")
        assert resp.status_code == 200
        assert resp.json() == {"page": "live"}


def test_calling_unattached_stub_raises():
    app = Veloce(debug=True)
    app.add_url_rule("/empty", endpoint="empty")

    with TestClient(app) as client:
        # No handler attached → the stub raises RuntimeError → 500.
        resp = client.get("/empty")
        assert resp.status_code == 500


def test_add_url_rule_with_view_func_still_works():
    app = Veloce()

    async def handler():
        return {"ok": True}

    app.add_url_rule("/normal", endpoint="normal", view_func=handler)
    with TestClient(app) as client:
        assert client.get("/normal").json() == {"ok": True}
