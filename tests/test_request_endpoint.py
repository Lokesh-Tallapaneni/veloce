"""Request.endpoint — matched route name."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce


def _req(path: str, method: str = "GET") -> Request:
    return Request(method=method, path=path, query_string="", headers={}, body=b"")


def test_synthetic_request_endpoint_is_none():
    req = _req("/x")
    assert req.endpoint is None


@pytest.mark.asyncio
async def test_endpoint_set_to_route_name():
    app = Veloce(debug=True, openapi_url=None)
    seen: dict = {}

    @app.get("/hello", name="say_hi")
    async def hi(request):
        seen["ep"] = request.endpoint
        return {}

    await app.handle_request(_req("/hello"))
    assert seen["ep"] == "say_hi"


@pytest.mark.asyncio
async def test_endpoint_defaults_to_handler_name():
    """When no explicit `name=` is set, the handler's __name__ is used."""
    app = Veloce(debug=True, openapi_url=None)
    seen: dict = {}

    @app.get("/x")
    async def my_handler(request):
        seen["ep"] = request.endpoint
        return {}

    await app.handle_request(_req("/x"))
    assert seen["ep"] == "my_handler"


@pytest.mark.asyncio
async def test_endpoint_available_in_before_request_hook():
    """`endpoint` is set before before_request runs."""
    app = Veloce(debug=True, openapi_url=None)
    seen: dict = {}

    @app.before_request
    def capture(request):
        seen["ep"] = request.endpoint

    @app.get("/x", name="my_route")
    async def x(request):
        return {}

    await app.handle_request(_req("/x"))
    assert seen["ep"] == "my_route"
