"""The HTTP door and the MCP door derive the same contract from a return annotation.

Two functions resolve it: `resolve_return_model` (the MCP door, `outputSchema`)
and `resolve_response_contract` (the HTTP door, `response_model` and OpenAPI).
They shared a `get_type_hints` preamble and the same base model checks, written
out twice - and the copies had diverged: the HTTP one dropped the
adaptable-model arm, so

    async def handler() -> SomeDataclass: ...

produced an MCP `outputSchema` and **no** HTTP response contract, leaving the
return unfiltered. `resolve_return_model`'s docstring called itself the "single
source of the return-annotation contract" while the HTTP door called the other
function.

The base checks now live in one `_base_return_model` that both reach, so the
narrower resolver cannot accept a shape the wider one drops. These tests assert
the two **against each other** across the annotation space, which is the property
the shared helper exists to guarantee - a test that checked each separately is
what let them drift.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import BaseModel

# `typing_extensions`, not `typing`: below Python 3.12 Pydantic refuses a
# `typing.TypedDict` outright - only the backport records the required/optional
# keys it needs - so `_typeddict_is_adaptable` correctly answers False for one
# there and the contract degrades to none. Declaring the shape the way the
# framework supports it keeps these tests about the contract rather than about
# which interpreter is running.
from typing_extensions import TypedDict

from veloce import Response, Veloce
from veloce._model_backend import resolve_response_contract, resolve_return_model
from veloce.contrib.mcp.registry import build_registry


class Model(BaseModel):
    a: int


@dataclass
class Point:
    x: int
    y: int


class Payload(TypedDict):
    name: str


# Annotations both doors must agree carry a contract.
SHARED = [Model, Point, Payload]

# Annotations neither door may treat as a contract.
NEITHER = [Response, dict, None, int, str, object]

# Shapes only the HTTP door widens to.
WIDENED = [list[Model], Model | None]


def _handler(annotation):
    async def handler():
        return None

    handler.__annotations__["return"] = annotation
    return handler


# ── the base is shared ───────────────────────────────────────────────


@pytest.mark.parametrize("annotation", SHARED)
def test_both_doors_accept_the_same_base_shapes(annotation):
    """The defect: the HTTP door dropped the dataclass / TypedDict arm."""
    handler = _handler(annotation)
    assert resolve_return_model(handler) is annotation
    assert resolve_response_contract(handler) is annotation


@pytest.mark.parametrize("annotation", NEITHER)
def test_neither_door_invents_a_contract(annotation):
    """The negative: an unrepresentable return declares nothing, on both doors."""
    handler = _handler(annotation)
    assert resolve_return_model(handler) is None
    assert resolve_response_contract(handler) is None


@pytest.mark.parametrize("annotation", SHARED + NEITHER)
def test_the_narrower_resolver_never_accepts_what_the_wider_one_drops(annotation):
    """The property the shared base exists to guarantee, stated directly."""
    handler = _handler(annotation)
    narrow = resolve_return_model(handler)
    wide = resolve_response_contract(handler)
    if narrow is not None:
        assert wide is not None, annotation


# ── and the widening is the HTTP door's alone ────────────────────────


@pytest.mark.parametrize("annotation", WIDENED)
def test_the_http_door_widens_to_route_only_shapes(annotation):
    # `==`, not `is`: a parameterised generic is not identity-stable, so
    # `list[Model] is list[Model]` may be False.
    handler = _handler(annotation)
    assert resolve_response_contract(handler) == annotation


@pytest.mark.parametrize("annotation", WIDENED)
def test_the_widened_shapes_are_not_base_models(annotation):
    """`list[Model]` is a route shape, not a model; the base resolver says so."""
    assert resolve_return_model(_handler(annotation)) is None


def test_a_list_of_non_models_is_not_a_contract():
    assert resolve_response_contract(_handler(list[int])) is None


def test_a_union_with_a_non_model_is_not_a_contract():
    assert resolve_response_contract(_handler(Model | int)) is None


# ── end to end, through both doors ───────────────────────────────────


def test_a_dataclass_return_reaches_both_doors():
    """The reported symptom, through the public surfaces."""
    app = Veloce(title="T", version="1")

    @app.get("/p")
    async def http_point() -> Point:
        return Point(1, 2)

    @app.mcp_tool(description="Return a point")
    async def mcp_point() -> Point:
        return Point(3, 4)

    info = next(i for _m, p, i in app._collect_all_routes() if p == "/p")
    assert info.response_model is Point

    tool = build_registry(app).tools["mcp_point"]
    assert tool.output_schema is not None
    assert set(tool.output_schema.get("properties", {})) == {"x", "y"}


def test_an_unreadable_annotation_declares_nothing_on_either_door():
    """A `get_type_hints` failure must degrade, not raise."""

    async def handler():
        return None

    handler.__annotations__["return"] = "NotAResolvableName"
    assert resolve_return_model(handler) is None
    assert resolve_response_contract(handler) is None


def test_a_handler_with_no_return_annotation_declares_nothing():
    async def handler():
        return None

    assert resolve_return_model(handler) is None
    assert resolve_response_contract(handler) is None
