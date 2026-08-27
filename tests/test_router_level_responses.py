"""R6 — router-level `responses=` merge into each route's OpenAPI."""

from __future__ import annotations

from veloce import Blueprint, Veloce


def test_router_responses_merged_into_route():
    bp = Blueprint(
        "api",
        url_prefix="/api",
        responses={
            403: {"description": "Forbidden"},
            422: {"description": "Validation Error"},
        },
    )

    @bp.get("/x")
    async def x():
        return {}

    app = Veloce(debug=True, openapi_url="/openapi.json")
    app.register_blueprint(bp)

    from veloce.contrib.openapi import get_openapi_schema

    schema = get_openapi_schema(app)
    op = schema["paths"]["/api/x"]["get"]
    assert "403" in op["responses"]
    assert op["responses"]["403"]["description"] == "Forbidden"
    assert "422" in op["responses"]


def test_route_responses_override_router_level():
    bp = Blueprint(
        "api",
        url_prefix="/api",
        responses={403: {"description": "Router-default"}},
    )

    @bp.get("/x", responses={403: {"description": "Route-specific"}})
    async def x():
        return {}

    app = Veloce(debug=True, openapi_url="/openapi.json")
    app.register_blueprint(bp)

    from veloce.contrib.openapi import get_openapi_schema

    schema = get_openapi_schema(app)
    op = schema["paths"]["/api/x"]["get"]
    assert op["responses"]["403"]["description"] == "Route-specific"


def test_router_responses_apply_to_every_route():
    bp = Blueprint(
        "api",
        url_prefix="/api",
        responses={500: {"description": "Server Error"}},
    )

    @bp.get("/a")
    async def a():
        return {}

    @bp.get("/b")
    async def b():
        return {}

    app = Veloce(debug=True, openapi_url="/openapi.json")
    app.register_blueprint(bp)

    from veloce.contrib.openapi import get_openapi_schema

    schema = get_openapi_schema(app)
    assert "500" in schema["paths"]["/api/a"]["get"]["responses"]
    assert "500" in schema["paths"]["/api/b"]["get"]["responses"]


def test_no_router_responses_yields_empty_route_responses():
    """When neither router nor route declares responses, the route's
    `responses` is the empty dict — RouteInfo normalises None to {}."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return {}

    match = app.match("GET", "/x")
    assert match is not None
    assert match.route_info.responses == {}


async def test_router_responses_inherited_via_nested_blueprint():
    parent = Blueprint("p", url_prefix="/p", responses={503: {"description": "Down"}})
    child = Blueprint("c", url_prefix="/c")

    @child.get("/x")
    async def x():
        return {}

    parent.register_blueprint(child)
    # After nesting, the child's route was re-registered onto `parent`,
    # so the parent's router-level responses apply to it.
    app = Veloce(debug=True, openapi_url="/openapi.json")
    app.register_blueprint(parent)

    from veloce.contrib.openapi import get_openapi_schema

    schema = get_openapi_schema(app)
    op = schema["paths"]["/p/c/x"]["get"]
    assert "503" in op["responses"]
