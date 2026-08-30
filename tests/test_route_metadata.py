"""Tests for route-level OpenAPI metadata (R26)."""

from __future__ import annotations

from tests._openapi import document
from tests.conftest import make_request
from veloce import Request, Veloce
from veloce.contrib.openapi import get_openapi_schema
from veloce.testclient import TestClient

# ── R26: operation_id ─────────────────────────────────────────────────


def test_operation_id_override_appears_in_openapi():
    app = Veloce(debug=True)

    @app.get("/items", operation_id="list_items_v1")
    async def list_items():
        return []

    client = TestClient(app)
    spec = document(client)
    op = spec["paths"]["/items"]["get"]
    assert op["operationId"] == "list_items_v1"


def test_operation_id_defaults_to_name_underscore_method():
    """No override → fallback to `<name>_<method>` (one-time stable id)."""
    app = Veloce(debug=True)

    @app.get("/items")
    async def list_items():
        return []

    client = TestClient(app)
    spec = document(client)
    op = spec["paths"]["/items"]["get"]
    assert op["operationId"] == "list_items_get"


def test_operation_id_works_via_route_decorator():
    """The generic `@router.route(...)` decorator also accepts operation_id."""
    app = Veloce(debug=True)

    @app.route("/x", methods=["POST"], operation_id="create_x_explicit")
    async def x():
        return {}

    spec = document(app)
    op = spec["paths"]["/x"]["post"]
    assert op["operationId"] == "create_x_explicit"


# ── include_in_schema ────────────────────────────────────────────────
#
# Moved here from `test_response_model_filtering.py`, where it sat under a
# section literally headed "unrelated route options this module also covers".
# It is route-level OpenAPI metadata, which is this module's subject; the
# parameter-level flag of the same name is `test_param_include_in_schema.py`.


async def test_include_in_schema_false():
    app = Veloce(openapi_url=None)

    @app.get("/internal", include_in_schema=False)
    async def internal(request: Request):
        return {"secret": True}

    schema = get_openapi_schema(app)
    assert "/internal" not in schema["paths"]

    # But route still works
    resp = await app.handle_request(make_request(path="/internal"))
    assert resp.status_code == 200
