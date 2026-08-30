"""Tests for route-level OpenAPI metadata (R26)."""

from __future__ import annotations

from veloce import Veloce
from veloce.testclient import TestClient

# ── R26: operation_id ─────────────────────────────────────────────────


def test_operation_id_override_appears_in_openapi():
    app = Veloce(debug=True)

    @app.get("/items", operation_id="list_items_v1")
    async def list_items():
        return []

    client = TestClient(app)
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/items"]["get"]
    assert op["operationId"] == "list_items_v1"


def test_operation_id_defaults_to_name_underscore_method():
    """No override → fallback to `<name>_<method>` (one-time stable id)."""
    app = Veloce(debug=True)

    @app.get("/items")
    async def list_items():
        return []

    client = TestClient(app)
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/items"]["get"]
    assert op["operationId"] == "list_items_get"


def test_operation_id_works_via_route_decorator():
    """The generic `@router.route(...)` decorator also accepts operation_id."""
    app = Veloce(debug=True)

    @app.route("/x", methods=["POST"], operation_id="create_x_explicit")
    async def x():
        return {}

    spec = TestClient(app).get("/openapi.json").json()
    op = spec["paths"]["/x"]["post"]
    assert op["operationId"] == "create_x_explicit"
