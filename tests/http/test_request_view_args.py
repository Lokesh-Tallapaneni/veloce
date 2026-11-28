"""Request.view_args — an alias for path_params."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Request, Veloce
from veloce.testclient import TestClient


def test_view_args_empty_by_default():
    req = make_request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.view_args == {}


def test_view_args_aliases_path_params():
    req = make_request(method="GET", path="/", query_string="", headers={}, body=b"")
    req.path_params = {"id": "7"}
    assert req.view_args == {"id": "7"}
    assert req.view_args is req.path_params


def test_view_args_populated_after_match():
    app = Veloce(openapi_url=None)
    captured: dict = {}

    @app.get("/items/{item_id:int}")
    async def item(request: Request, item_id: int):
        captured["view_args"] = dict(request.view_args)
        return {}

    with TestClient(app) as client:
        client.get("/items/42")

    assert captured["view_args"] == {"item_id": 42}


def test_path_params_populated_on_bare_fast_path():
    """A request-only handler on a bare app still sees `request.path_params`.

    The straight-line fast path must assign the matched params to the request,
    not only the slow (`_resolve_route`) path. A request-only handler keeps the
    route fast-eligible, so this exercises the fast path with a parameterized
    route on an app with no middleware/hooks.
    """
    app = Veloce(openapi_url=None)
    captured: dict = {}

    @app.get("/items/{item_id}")
    async def item(request: Request):
        captured["path_params"] = dict(request.path_params)
        return {}

    with TestClient(app) as client:
        client.get("/items/42")

    assert captured["path_params"] == {"item_id": "42"}
