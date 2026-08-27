"""A dataclass return annotation is a response contract, like every other model.

`_model_backend` has two return-annotation resolvers. `resolve_return_model`
feeds the MCP door's `outputSchema`; `resolve_response_contract` feeds the HTTP
door's `response_model` and OpenAPI. The second's docstring says it "widens
`resolve_return_model`", and it does not — it drops the adaptable models
(dataclass, `TypedDict`) the first accepts:

| annotation | MCP `outputSchema` | HTTP `response_model` |
|---|---|---|
| a Pydantic model | yes | yes |
| **a dataclass** | **yes** | **None** |
| **a `TypedDict`** | **yes** | **None** |
| `list[Model]` | None | yes |
| `Model \\| None` | None | yes |

Two bugs fell out of reproducing that, and the second is worse than the report:

1. **`-> SomeDataclass` leaked.** No HTTP contract meant no filtering, so a
   richer object returned under a narrower dataclass annotation put its extra
   fields on the wire — the same leak `response_model` exists to prevent, and the
   same one fixed for msgspec earlier in this review.
2. **`response_model=SomeDataclass` was a `500`.** Not a leak, a hard failure:
   `TypeAdapter.validate_python` refuses an unrelated dataclass instance
   outright ("Input should be a dictionary or an instance of Public"). The
   dump-before-validate step that the Pydantic branch of `shape_through_model`
   already had has no equivalent for adaptable models.

The `list[Model]` and `Model | None` rows stay as they are, deliberately: an MCP
`outputSchema` must be an object schema, and neither a bare array nor an
alternation is one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

import pytest
from pydantic import BaseModel, ValidationError

from veloce import JSONResponse, Veloce
from veloce._model_backend import (
    resolve_response_contract,
    resolve_return_model,
    shape_through_model,
)
from veloce.testclient import TestClient


@dataclass
class Public:
    id: int


@dataclass
class Private:
    id: int
    secret: str


class PublicDict(TypedDict):
    id: int


def _serve(value, *, model=None, annotate=None):
    """Serve `value` from a route declaring its contract one way or the other.

    The annotation is set on `__annotations__` rather than written as `-> X`.
    This module carries `from __future__ import annotations`, so a `-> X` naming
    a local would be the string `"X"` and `get_type_hints` could not resolve it -
    which would make these tests measure the helper rather than the framework.
    """
    app = Veloce(openapi_url=None)

    async def route():
        return value

    if annotate is not None:
        route.__annotations__["return"] = annotate
        app.get("/r")(route)
    else:
        app.get("/r", response_model=model)(route)
    return TestClient(app).get("/r")


# ── the leak ─────────────────────────────────────────────────────────


def test_a_dataclass_return_annotation_filters_the_response():
    """The defect: `secret` reached the wire."""
    assert _serve(Private(id=1, secret="LEAK"), annotate=Public).json() == {"id": 1}


def test_a_typed_dict_return_annotation_filters_the_response():
    assert _serve({"id": 1, "secret": "LEAK"}, annotate=PublicDict).json() == {"id": 1}


def test_a_dataclass_return_annotation_serves_a_conforming_value():
    assert _serve(Public(id=7), annotate=Public).json() == {"id": 7}


def test_a_dataclass_return_annotation_accepts_a_plain_dict():
    assert _serve({"id": 3, "extra": "x"}, annotate=Public).json() == {"id": 3}


# ── the 500 ──────────────────────────────────────────────────────────


def test_an_explicit_dataclass_response_model_does_not_error():
    """The defect: this was a 500, not a filtered body."""
    resp = _serve(Private(id=1, secret="LEAK"), model=Public)
    assert resp.status_code == 200


def test_an_explicit_dataclass_response_model_filters_the_response():
    assert _serve(Private(id=1, secret="LEAK"), model=Public).json() == {"id": 1}


def test_an_explicit_dataclass_response_model_serves_a_conforming_value():
    assert _serve(Public(id=7), model=Public).json() == {"id": 7}


def test_the_two_spellings_agree():
    """A declared contract must mean the same thing however it was declared."""
    explicit = _serve(Private(id=1, secret="LEAK"), model=Public).json()
    inferred = _serve(Private(id=1, secret="LEAK"), annotate=Public).json()
    assert explicit == inferred == {"id": 1}


# ── the shaper, directly ─────────────────────────────────────────────


def test_the_shaper_filters_an_unrelated_dataclass_instance():

    assert shape_through_model(Private(id=1, secret="s"), Public) == {"id": 1}


def test_the_shaper_still_filters_a_mapping():

    assert shape_through_model({"id": 1, "extra": "x"}, Public) == {"id": 1}


def test_the_shaper_still_refuses_a_non_conforming_value():
    """Filtering must not become "accept anything"."""
    with pytest.raises(ValidationError):
        shape_through_model({"nope": 1}, Public)


def test_the_shaper_refuses_a_value_of_the_wrong_type():
    with pytest.raises(ValidationError):
        shape_through_model({"id": "not-an-int"}, Public)


# ── the two resolvers ────────────────────────────────────────────────


@pytest.mark.parametrize("model", [Public, PublicDict])
def test_both_resolvers_accept_an_adaptable_model(model):
    """`resolve_response_contract` documents itself as a widening of the other;
    dropping a shape the other accepts is the opposite of that."""

    def handler() -> model: ...

    handler.__annotations__["return"] = model
    assert resolve_return_model(handler) is model
    assert resolve_response_contract(handler) is model


def test_a_pydantic_return_is_accepted_by_both():
    class M(BaseModel):
        id: int

    def handler() -> M: ...

    handler.__annotations__["return"] = M
    assert resolve_return_model(handler) is M
    assert resolve_response_contract(handler) is M


def test_the_http_resolver_is_a_superset_of_the_mcp_one():
    """The property the docstring claims. Asserted over every shape both see."""

    class M(BaseModel):
        id: int

    for annotation in (M, Public, PublicDict, list[M], M | None, dict, None, int, str):

        def handler(): ...

        handler.__annotations__["return"] = annotation
        mcp = resolve_return_model(handler)
        http = resolve_response_contract(handler)
        if mcp is not None:
            assert http is not None, f"{annotation!r}: MCP has a contract and HTTP does not"


# ── the narrowings that stay ─────────────────────────────────────────
#
# The negatives. An MCP `outputSchema` must be an object schema, so neither a
# bare array nor an alternation qualifies - the MCP resolver declining them is
# not the same class of gap as dropping a dataclass.


def test_a_list_return_is_an_http_contract_but_not_an_mcp_one():
    class M(BaseModel):
        id: int

    def handler(): ...

    handler.__annotations__["return"] = list[M]
    assert resolve_return_model(handler) is None
    assert resolve_response_contract(handler) is not None


def test_a_union_return_is_an_http_contract_but_not_an_mcp_one():
    class M(BaseModel):
        id: int

    def handler(): ...

    handler.__annotations__["return"] = M | None
    assert resolve_return_model(handler) is None
    assert resolve_response_contract(handler) is not None


@pytest.mark.parametrize("annotation", [dict, int, str, None])
def test_an_unrepresentable_return_declares_no_contract_on_either_door(annotation):
    def handler(): ...

    handler.__annotations__["return"] = annotation
    assert resolve_return_model(handler) is None
    assert resolve_response_contract(handler) is None


def test_a_response_return_annotation_declares_no_contract():
    """A transport class is a shape, not a model - the opt-out that needs no opt-out."""

    def handler(): ...

    handler.__annotations__["return"] = JSONResponse
    assert resolve_return_model(handler) is None
    assert resolve_response_contract(handler) is None
