"""Router.default_response_class (D10)."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import JSONResponse, ORJSONResponse, Request, Veloce


def _req(path: str = "/") -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


async def test_default_response_class_used_when_route_has_none():
    app = Veloce(debug=True, openapi_url=None, default_response_class=ORJSONResponse)

    @app.get("/x")
    async def x():
        return {"ok": True}

    resp = await app.handle_request(_req("/x"))
    assert isinstance(resp, ORJSONResponse)


async def test_route_response_class_overrides_default():
    app = Veloce(debug=True, openapi_url=None, default_response_class=ORJSONResponse)

    @app.get("/x", response_class=JSONResponse)
    async def x():
        return {"ok": True}

    resp = await app.handle_request(_req("/x"))
    # JSONResponse, not ORJSONResponse — per-route wins.
    assert type(resp) is JSONResponse


async def test_no_default_falls_back_to_jsonresponse():
    """Without default_response_class, dict returns get JSONResponse as before."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {"ok": True}

    resp = await app.handle_request(_req("/x"))
    assert isinstance(resp, JSONResponse)


async def test_default_propagates_to_decorator_kwargless_route():
    """The default applies even when the @get decorator gets no kwargs."""
    app = Veloce(debug=True, openapi_url=None, default_response_class=ORJSONResponse)

    @app.get("/y")
    async def y():
        return [1, 2, 3]

    resp = await app.handle_request(_req("/y"))
    assert isinstance(resp, ORJSONResponse)
