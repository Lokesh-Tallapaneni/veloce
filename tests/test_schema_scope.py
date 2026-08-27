"""The schema describes the app, not the server's own documentation routes.

`/openapi.json`, `/docs` and `/redoc` were registered without
`include_in_schema=False`, so every generated document carried three operations
for the server's own documentation endpoints:

    GET /x            operationId=x_get
    GET /openapi.json operationId=openapi_schema_get   tags=['openapi']
    GET /docs         operationId=swagger_ui_get       tags=['openapi']
    GET /redoc        operationId=redoc_ui_get         tags=['openapi']

A generated client grew a method for fetching the schema it was generated from,
and two for rendering HTML pages. `paths` describes the application's API; these
three are not part of it.
"""

from __future__ import annotations

import pytest

from tests._openapi import document
from veloce import Veloce
from veloce.testclient import TestClient


def _app(**kwargs) -> Veloce:
    app = Veloce(**kwargs)

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    return app


# ── the schema describes the app, not the docs ───────────────────────


def test_the_schema_lists_only_the_apps_own_routes():
    """The defect: three documentation operations in every document."""
    assert list(document(_app())["paths"]) == ["/x"]


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_a_documentation_route_is_not_in_the_schema(path):
    assert path not in document(_app())["paths"]


def test_no_operation_carries_the_openapi_tag():
    """That tag existed only on the three routes that are now excluded."""
    schema = document(_app())
    tags = [op.get("tags") for ops in schema["paths"].values() for op in ops.values()]
    assert all(t is None or "openapi" not in t for t in tags)


def test_custom_documentation_paths_are_excluded_too():
    app = _app(docs_url="/documentation", redoc_url="/reference", openapi_url="/schema.json")
    schema = TestClient(app).get("/schema.json").json()
    assert list(schema["paths"]) == ["/x"]


def test_a_prefixed_app_excludes_them_too():
    app = _app(prefix="/api")
    schema = TestClient(app).get("/api/openapi.json").json()
    assert list(schema["paths"]) == ["/api/x"]


# ── the pages themselves still work ──────────────────────────────────


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_a_documentation_route_is_still_served(path):
    """Excluded from the document, not from the app."""
    assert TestClient(_app()).get(path).status_code == 200


def test_the_docs_page_still_finds_the_schema():
    """The page fetches the schema at runtime; exclusion must not break that."""
    client = TestClient(_app())
    page = client.get("/docs").text
    assert "/openapi.json" in page
    assert client.get("/openapi.json").status_code == 200


def test_the_apps_own_route_is_still_described():
    schema = document(_app())
    assert schema["paths"]["/x"]["get"]["operationId"] == "x_get"


def test_a_route_asking_to_be_excluded_still_is():
    """The mechanism is unchanged; it is only applied to three more routes."""
    app = Veloce()

    @app.get("/public")
    async def public() -> dict:
        return {}

    @app.get("/internal", include_in_schema=False)
    async def internal() -> dict:
        return {}

    assert list(document(app)["paths"]) == ["/public"]


def test_an_app_with_no_routes_has_an_empty_paths_object():
    """Previously it had three - its own documentation endpoints."""
    app = Veloce()
    assert document(app)["paths"] == {}


def test_the_document_is_still_valid():
    app = Veloce(validate_openapi=True)

    @app.get("/x")
    async def x() -> dict:
        return {}

    schema = app.openapi()
    assert schema["info"]["title"]
    assert "paths" in schema
