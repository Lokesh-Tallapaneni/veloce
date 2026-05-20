"""Route-level openapi_extra."""

from __future__ import annotations

from veloce import Veloce
from veloce.contrib.openapi import get_openapi_schema


def test_openapi_extra_merges_top_level_key():
    app = Veloce()

    @app.get("/x", openapi_extra={"x-internal": True})
    async def x():
        return {}

    schema = get_openapi_schema(app)
    op = schema["paths"]["/x"]["get"]
    assert op["x-internal"] is True


def test_openapi_extra_deep_merges_nested():
    app = Veloce()

    @app.get(
        "/y",
        openapi_extra={"responses": {"418": {"description": "teapot"}}},
    )
    async def y():
        return {}

    schema = get_openapi_schema(app)
    op = schema["paths"]["/y"]["get"]
    # Generated 200 still present; 418 merged in.
    assert "200" in op["responses"]
    assert op["responses"]["418"]["description"] == "teapot"


def test_openapi_extra_overrides_scalar():
    app = Veloce()

    @app.get("/z", summary="generated", openapi_extra={"summary": "overridden"})
    async def z():
        return {}

    schema = get_openapi_schema(app)
    assert schema["paths"]["/z"]["get"]["summary"] == "overridden"


def test_openapi_extra_absent_leaves_operation_unchanged():
    app = Veloce()

    @app.get("/plain")
    async def plain():
        return {}

    schema = get_openapi_schema(app)
    op = schema["paths"]["/plain"]["get"]
    assert "x-internal" not in op
    assert op["summary"] == "plain"


def test_openapi_extra_on_post():
    app = Veloce()

    @app.post("/create", openapi_extra={"x-rate-limit": 100})
    async def create():
        return {}

    schema = get_openapi_schema(app)
    assert schema["paths"]["/create"]["post"]["x-rate-limit"] == 100


def test_openapi_extra_injects_custom_request_body():
    """The canonical the canonical use case: hand-rolled requestBody."""
    app = Veloce()

    custom_body = {"requestBody": {"content": {"application/yaml": {"schema": {"type": "string"}}}}}

    @app.post("/yaml", openapi_extra=custom_body)
    async def yaml_endpoint():
        return {}

    schema = get_openapi_schema(app)
    op = schema["paths"]["/yaml"]["post"]
    assert "application/yaml" in op["requestBody"]["content"]
