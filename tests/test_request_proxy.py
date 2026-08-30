"""`request` context-local proxy."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce, request
from veloce.testclient import TestClient


def test_request_proxy_raises_outside_context():
    with pytest.raises(RuntimeError, match="outside of request context"):
        _ = request.method


def test_request_proxy_falsy_outside_context():
    assert not request


def test_request_proxy_resolves_during_dispatch():
    app = Veloce()
    seen: dict = {}

    @app.get("/x")
    async def x(req: Request):
        # The global `request` proxy resolves to the live request.
        seen["method"] = request.method
        seen["path"] = request.path
        return {}

    with TestClient(app) as client:
        client.get("/x")

    assert seen == {"method": "GET", "path": "/x"}


def test_request_proxy_inside_test_request_context():
    app = Veloce()
    with app.test_request_context(path="/probe", method="POST"):
        assert request.method == "POST"
        assert request.path == "/probe"
        assert bool(request) is True


def test_request_proxy_repr_unbound():
    assert "unbound" in repr(request)
