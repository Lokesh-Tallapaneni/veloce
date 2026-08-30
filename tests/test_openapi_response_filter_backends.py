"""`response_model_include` / `exclude` document the filtered shape, on both backends.

`_filtered_response_model` derives a model carrying only the fields the route
actually sends, so the document does not mark an omitted field `required`. It
did so by reading `model.model_fields`, which only a Pydantic model has - but
the gate that reaches it, `_is_model_type`, also admits a `msgspec.Struct`.

A single route declaring a Struct response model *and* a filter therefore made
`app.openapi()` raise `AttributeError`, so `/openapi.json` and `/docs` returned
500 for the whole application, not just that route. The `if not include and not
exclude: return model` guard is why an unfiltered Struct route never hit it, and
why the suite did not.
"""

from __future__ import annotations

import msgspec
import pytest
from pydantic import BaseModel

from tests._openapi import document
from veloce import Veloce
from veloce.contrib._jsonschema import _is_model_type


class ItemModel(BaseModel):
    name: str
    secret: str


class ItemStruct(msgspec.Struct):
    name: str
    secret: str


def _app_returning(model: type, **route_kwargs) -> Veloce:
    app = Veloce(openapi_url="/openapi.json")

    @app.get("/i", response_model=model, **route_kwargs)
    async def i():  # pragma: no cover - the document is the subject
        return {"name": "n", "secret": "s"}

    return app


@pytest.mark.parametrize("model", [ItemModel, ItemStruct], ids=["pydantic", "msgspec"])
def test_a_filtered_response_model_still_builds_a_document(model):
    """The regression: a Struct plus a filter raised, taking the whole document."""
    app = _app_returning(model, response_model_exclude={"secret"})

    schema = app.openapi()

    assert "/i" in schema["paths"]


@pytest.mark.parametrize("model", [ItemModel, ItemStruct], ids=["pydantic", "msgspec"])
def test_the_document_is_served_rather_than_500(model):
    """Through the client, because that is how a user meets this."""
    app = _app_returning(model, response_model_exclude={"secret"})

    assert "/i" in document(app)["paths"]


@pytest.mark.parametrize("model", [ItemModel, ItemStruct], ids=["pydantic", "msgspec"])
def test_an_unfiltered_route_is_unaffected(model):
    """The guard that kept the bug rare: no filter, no derivation."""
    assert "/i" in _app_returning(model).openapi()["paths"]


def test_the_excluded_field_is_gone_from_the_pydantic_schema():
    """The behaviour the derivation exists for, on the backend that had it."""
    schema = _app_returning(ItemModel, response_model_exclude={"secret"}).openapi()
    components = schema.get("components", {}).get("schemas", {})
    filtered = [body for name, body in components.items() if "secret" not in body["properties"]]

    assert filtered, "no component documents the filtered shape"
    assert all("name" in body["properties"] for body in filtered)


def test_both_backends_reach_the_same_gate():
    """The premise: if `_is_model_type` stopped admitting Structs, these would
    pass for a reason that has nothing to do with the fix."""
    assert _is_model_type(ItemModel)
    assert _is_model_type(ItemStruct)
