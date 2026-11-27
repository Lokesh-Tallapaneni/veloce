"""Route-level callbacks → OpenAPI emission."""

from __future__ import annotations

from tests._routes import route_at
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

    assert route_at(app, "/x").callbacks == _CALLBACK


def test_callbacks_default_none():
    app = Veloce()

    @app.get("/y")
    async def y():
        return {}

    assert route_at(app, "/y").callbacks is None
