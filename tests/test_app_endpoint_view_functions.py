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


@pytest.mark.asyncio
async def test_endpoint_decorator_reclassifies_sync_handler():
    """Attaching a sync view to a stub route must reclassify the route -
    the stub is async (fast-path eligible on a bare app), the replacement is
    sync and must be offloaded, never awaited."""
    app = Veloce(openapi_url=None)
    app.add_url_rule("/x", endpoint="my_view")

    @app.endpoint("my_view")
    def sync_handler():
        return {"v": "sync"}

    info = next(i for _m, _p, i in app._collect_all_routes() if i.name == "my_view")
    assert info.is_fast_eligible is False

    import orjson

    resp = await app.handle_request(_req())
    assert orjson.loads(resp.body) == {"v": "sync"}


@pytest.mark.asyncio
async def test_endpoint_decorator_keeps_async_fast_path():
    """An async replacement stays fast-path eligible on a bare app."""
    app = Veloce(openapi_url=None)
    app.add_url_rule("/x", endpoint="my_view")

    @app.endpoint("my_view")
    async def async_handler():
        return {"v": "async"}

    info = next(i for _m, _p, i in app._collect_all_routes() if i.name == "my_view")
    assert info.is_fast_eligible is True

    import orjson

    resp = await app.handle_request(_req())
    assert orjson.loads(resp.body) == {"v": "async"}


def test_endpoint_decorator_refreshes_view_functions():
    """A `view_functions` snapshot taken before `@app.endpoint` must not pin
    the displaced stub in the cache."""
    app = Veloce(openapi_url=None)
    app.add_url_rule("/x", endpoint="my_view", view_func=lambda: {})
    assert "my_view" in app.view_functions  # primes the cache

    @app.endpoint("my_view")
    async def new_handler():
        return {}

    assert app.view_functions["my_view"] is new_handler


def test_endpoint_decorator_unknown_name_raises():
    app = Veloce(openapi_url=None)

    with pytest.raises(ValueError, match="No route registered"):

        @app.endpoint("does_not_exist")
        def fn():
            return {}
