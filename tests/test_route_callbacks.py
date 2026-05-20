"""Route-level callbacks → OpenAPI emission (R27)."""

from __future__ import annotations

from veloce import Veloce
from veloce.contrib.openapi import get_openapi_schema

_CALLBACK = {
    "onEvent": {
        "{$request.body#/callbackUrl}": {
            "post": {
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"200": {"description": "ack"}},
            }
        }
    }
}


def test_callbacks_emitted_into_operation():
    app = Veloce()

    @app.post("/subscribe", callbacks=_CALLBACK)
    async def subscribe():
        return {}

    schema = get_openapi_schema(app)
    op = schema["paths"]["/subscribe"]["post"]
    assert op["callbacks"] == _CALLBACK


def test_no_callbacks_means_no_field():
    app = Veloce()

    @app.get("/plain")
    async def plain():
        return {}

    op = get_openapi_schema(app)["paths"]["/plain"]["get"]
    assert "callbacks" not in op


def test_callbacks_stored_on_route_info():
    app = Veloce()

    @app.post("/x", callbacks=_CALLBACK)
    async def x():
        return {}

    for _m, path, info in app._collect_all_routes():
        if path == "/x":
            assert info.callbacks == _CALLBACK
            break
    else:
        raise AssertionError("route not found")


def test_callbacks_default_none():
    app = Veloce()

    @app.get("/y")
    async def y():
        return {}

    for _m, path, info in app._collect_all_routes():
        if path == "/y":
            assert info.callbacks is None
            break
    else:
        raise AssertionError("route not found")
