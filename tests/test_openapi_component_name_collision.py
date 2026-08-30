"""A top-level component does not overwrite a nested `$defs` entry of the same name.

`SchemaRegistry.finalize` wrote `components[name] = entry.body` with no collision
check, while the nested-def half of the same loop had one
(`_diverging_def_renames`). So a model reachable two ways - nested inside one
route's request body, and as another route's response model - had its request
schema replaced by its response schema:

    Inner: props=['a', 'derived'] required=['a', 'derived']
    Outer -> {"properties": {"inner": {"$ref": "#/components/schemas/Inner"}}, ...}

`derived` is a `computed_field`. The published request schema requires a
read-only field the model does not accept as input, so a client generated from
the document sends it and is rejected.

Not a regression: it reproduces on `main` too, and was carried into the
rewritten module rather than introduced by it.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, computed_field

from veloce import Veloce


class Inner(BaseModel):
    a: int

    @computed_field
    @property
    def derived(self) -> str:
        return str(self.a)


class Outer(BaseModel):
    inner: Inner


class Plain(BaseModel):
    """Used one way only, so its bare name must stay bare."""

    a: int


def _schemas(request_first: bool) -> dict:
    """Build the document with the two routes registered in either order."""
    app = Veloce(openapi_url="/openapi.json")

    def add_request_route():
        @app.post("/takes")
        async def takes(payload: Outer) -> dict:
            return {}

    def add_response_route():
        @app.get("/gives")
        async def gives() -> Inner:
            return Inner(a=1)

    if request_first:
        add_request_route()
        add_response_route()
    else:
        add_response_route()
        add_request_route()

    return app.openapi()["components"]["schemas"]


def _ref_target(schemas: dict) -> dict:
    """The schema `Outer.inner` actually points at."""
    ref = schemas["Outer"]["properties"]["inner"]["$ref"]
    return schemas[ref.rsplit("/", 1)[-1]]


@pytest.mark.parametrize("request_first", [True, False], ids=["request-first", "response-first"])
def test_the_request_schema_does_not_require_a_computed_field(request_first: bool):
    """The regression, from both registration orders."""
    schemas = _schemas(request_first)

    required = _ref_target(schemas).get("required", [])

    assert "derived" not in required, (
        "the published request body requires a read-only field the model rejects as input"
    )


@pytest.mark.parametrize("request_first", [True, False], ids=["request-first", "response-first"])
def test_the_request_schema_still_requires_the_real_field(request_first: bool):
    """The fix must not empty the schema to satisfy the one above."""
    assert "a" in _ref_target(_schemas(request_first)).get("required", [])


@pytest.mark.parametrize("request_first", [True, False], ids=["request-first", "response-first"])
def test_the_response_schema_still_carries_the_computed_field(request_first: bool):
    """Both shapes must survive: separating them is the point, not dropping one."""
    schemas = _schemas(request_first)

    carrying = [
        name
        for name, schema in schemas.items()
        if "derived" in schema.get("properties", {}) and "a" in schema.get("properties", {})
    ]

    assert carrying, "the serialization shape was lost"


@pytest.mark.parametrize("request_first", [True, False], ids=["request-first", "response-first"])
def test_the_two_shapes_are_separate_components(request_first: bool):
    """One name cannot describe both, which is the whole defect."""
    schemas = _schemas(request_first)
    with_derived = {
        name for name, schema in schemas.items() if "derived" in schema.get("properties", {})
    }
    without = {
        name
        for name, schema in schemas.items()
        if "a" in schema.get("properties", {}) and "derived" not in schema.get("properties", {})
    }

    assert with_derived and without
    assert not (with_derived & without)


@pytest.mark.parametrize("request_first", [True, False], ids=["request-first", "response-first"])
def test_every_ref_in_the_document_resolves(request_first: bool):
    """Renaming a component must not orphan a reference to it."""
    schemas = _schemas(request_first)

    def refs(node):
        if isinstance(node, dict):
            if isinstance(node.get("$ref"), str):
                yield node["$ref"]
            for value in node.values():
                yield from refs(value)
        elif isinstance(node, list):
            for item in node:
                yield from refs(item)

    for ref in refs(schemas):
        assert ref.startswith("#/components/schemas/"), ref
        assert ref.rsplit("/", 1)[-1] in schemas, f"dangling {ref}"


def test_a_model_used_only_one_way_keeps_its_bare_name():
    """The common case must not acquire a suffix it does not need."""
    app = Veloce(openapi_url="/openapi.json")

    @app.post("/p")
    async def p(payload: Plain) -> dict:
        return {}

    assert "Plain" in app.openapi()["components"]["schemas"]
