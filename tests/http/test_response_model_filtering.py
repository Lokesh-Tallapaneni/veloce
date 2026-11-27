"""`response_model_include` / `response_model_exclude` on routes.

The module is named for these two options and its one filtering test asserted
only that the path appeared in the OpenAPI document — nothing about the excluded
field, on the wire or in the schema — and `response_model_include` was never
tested at all. Both could have stopped working entirely with the module green.

Writing the assertions the name promised found a real defect behind them. The
wire filtering works, but the OpenAPI lowering knew nothing about either option,
so the documented contract contradicted what the route sent:

    @app.get("/inc", response_model=Item, response_model_include={"name"})

    wire:   {"name": "W"}
    schema: $ref Item, whose `required` is ["name", "price"]

A generated client is told `price` is always present and never receives it. That
is the same "two doors disagree" shape as the other schema-vs-runtime defects in
this review, and the reason a vacuous test is worse than no test: it occupied the
place where this would have been caught.
"""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Veloce
from veloce.testclient import TestClient


class Item(BaseModel):
    name: str
    price: float
    tax: float = 10.0


def _app(**route_kwargs) -> Veloce:
    app = Veloce(title="T", version="1")

    @app.get("/items/{item_id}", response_model=Item, **route_kwargs)
    async def get_item(item_id: int):
        return {"name": "Widget", "price": 9.99, "tax": 1.0}

    return app


def _wire(**route_kwargs) -> dict:
    return TestClient(_app(**route_kwargs)).get("/items/1").json()


def _response_schema(app: Veloce) -> dict:
    operation = app.openapi()["paths"]["/items/{item_id}"]["get"]
    return operation["responses"]["200"]["content"]["application/json"]["schema"]


def _resolve(app: Veloce, schema: dict) -> dict:
    """Follow a `$ref` into `components/schemas`, or return the schema as-is."""
    ref = schema.get("$ref")
    if ref is None:
        return schema
    return app.openapi()["components"]["schemas"][ref.rsplit("/", 1)[-1]]


# ── the wire ─────────────────────────────────────────────────────────


def test_exclude_drops_the_field_from_the_response():
    """The assertion the module was named for and never made."""
    assert _wire(response_model_exclude={"tax"}) == {"name": "Widget", "price": 9.99}


def test_include_keeps_only_the_named_fields():
    assert _wire(response_model_include={"name"}) == {"name": "Widget"}


def test_include_may_name_several_fields():
    assert _wire(response_model_include={"name", "price"}) == {
        "name": "Widget",
        "price": 9.99,
    }


def test_exclude_may_name_several_fields():
    assert _wire(response_model_exclude={"tax", "price"}) == {"name": "Widget"}


def test_no_filtering_sends_every_field():
    """The negative: the options must not be doing this by accident."""
    assert _wire() == {"name": "Widget", "price": 9.99, "tax": 1.0}


def test_excluding_nothing_sends_every_field():
    assert _wire(response_model_exclude=set()) == {
        "name": "Widget",
        "price": 9.99,
        "tax": 1.0,
    }


# ── and the document says the same thing ─────────────────────────────


def test_an_excluded_field_is_absent_from_the_documented_schema():
    """The defect: the schema still advertised the excluded field."""
    app = _app(response_model_exclude={"tax"})
    schema = _resolve(app, _response_schema(app))
    assert "tax" not in schema["properties"]


def test_an_included_field_set_is_the_documented_schema():
    app = _app(response_model_include={"name"})
    schema = _resolve(app, _response_schema(app))
    assert sorted(schema["properties"]) == ["name"]


def test_a_filtered_field_is_not_documented_as_required():
    """The sharp end: `price` has no default, so the unfiltered schema marks it
    required - and the route never sends it."""
    app = _app(response_model_include={"name"})
    schema = _resolve(app, _response_schema(app))
    assert "price" not in (schema.get("required") or [])


def test_the_documented_fields_are_exactly_the_sent_fields():
    """Stated once as the property both halves are really about."""
    for kwargs in (
        {"response_model_include": {"name"}},
        {"response_model_include": {"name", "price"}},
        {"response_model_exclude": {"tax"}},
        {"response_model_exclude": {"tax", "price"}},
        {},
    ):
        app = _app(**kwargs)
        documented = set(_resolve(app, _response_schema(app))["properties"])
        sent = set(_wire(**kwargs))
        assert sent <= documented, (kwargs, sent - documented)
        assert documented - sent <= {"tax"}, (kwargs, documented - sent)


def test_an_unfiltered_route_still_refs_the_shared_component():
    """Filtering emits its own shape; an unfiltered route must not.

    Otherwise every route would inline a copy of its model and the components
    section would stop being shared.
    """
    app = _app()
    assert "$ref" in _response_schema(app)


def test_a_required_field_that_survives_filtering_stays_required():
    app = _app(response_model_include={"name", "price"})
    schema = _resolve(app, _response_schema(app))
    assert sorted(schema.get("required") or []) == ["name", "price"]
