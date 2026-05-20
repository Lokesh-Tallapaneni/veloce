"""@app.endpoint(name) decorator + app.view_functions dict."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce


def _req(path: str = "/x") -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


def test_view_functions_lists_registered_handlers():
    app = Veloce(openapi_url=None)

    @app.get("/x", name="x_route")
    async def x():
        return {}

    @app.get("/y", name="y_route")
    async def y():
        return {}

    funcs = app.view_functions
    assert "x_route" in funcs
    assert "y_route" in funcs


def test_view_functions_defaults_to_handler_name():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def my_handler():
        return {}

    assert "my_handler" in app.view_functions


def test_view_functions_returns_snapshot():
    """Mutating the returned dict doesn't poison framework state."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def h():
        return {}

    funcs = app.view_functions
    funcs["bogus"] = lambda: None
    assert "bogus" not in app.view_functions


@pytest.mark.asyncio
async def test_endpoint_decorator_replaces_handler():
    """`@app.endpoint("name")` attaches a new handler to an existing route."""
    app = Veloce(debug=True, openapi_url=None)
    app.add_url_rule("/x", endpoint="my_view", view_func=lambda: {"v": "old"})

    @app.endpoint("my_view")
    async def new_handler():
        return {"v": "new"}

    import orjson

    resp = await app.handle_request(_req())
    assert orjson.loads(resp.body) == {"v": "new"}


def test_endpoint_decorator_unknown_name_raises():
    app = Veloce(openapi_url=None)

    with pytest.raises(ValueError, match="No route registered"):

        @app.endpoint("does_not_exist")
        def fn():
            return {}
