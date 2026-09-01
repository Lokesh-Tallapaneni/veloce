"""The model predicates agree, or disagree only where it is written down."""

from __future__ import annotations

import dataclasses
import json

import pytest
from pydantic import BaseModel

from veloce import Veloce
from veloce._model_backend import ModelBackend, backend_of
from veloce._route_contract import _is_model
from veloce.contrib._jsonschema import _is_model_type


@dataclasses.dataclass
class Point:
    x: int
    y: int


class PointModel(BaseModel):
    x: int


msgspec = pytest.importorskip("msgspec", reason="msgspec is not installed")


class PointStruct(msgspec.Struct):
    x: int


def test_the_two_predicates_agree_on_the_backend_backed_types():
    """POSITIVE: pydantic and msgspec are models to both gates."""
    for tp in (PointModel, PointStruct):
        assert _is_model_type(tp) is True, tp
        assert _is_model(tp) is True, tp
        assert backend_of(tp) is not ModelBackend.NONE, tp


def test_neither_predicate_admits_a_plain_mapping():
    """NEGATIVE: `dict` is not a model to anything, or the gate is meaningless."""
    assert _is_model_type(dict) is False
    assert _is_model(dict) is False
    assert backend_of(dict) is ModelBackend.NONE


def test_an_adapted_type_is_a_model_to_both_gates():
    """NEGATIVE: the divergence is pinned, so it cannot drift back silently.

    `_is_model_type` used to answer False here while `_is_model` answered True,
    and the schema emitter documented a dataclass response as having no content.
    """
    assert _is_model_type(Point) is True
    assert _is_model(Point) is True
    assert backend_of(Point) is ModelBackend.ADAPTED


def test_a_dataclass_response_model_is_documented_with_its_schema():
    """POSITIVE: the emitted document names the type the resolver returns."""
    app = Veloce()

    @app.get("/point")
    async def point() -> Point: ...

    content = app.openapi()["paths"]["/point"]["get"]["responses"]["200"]["content"]
    assert content["application/json"]["schema"] == {"$ref": "#/components/schemas/Point"}


def test_a_scalar_return_annotation_still_emits_no_component():
    """NEGATIVE: widening the gate must not turn every return type into a model."""
    app = Veloce()

    @app.get("/count")
    async def count() -> int: ...

    schema = app.openapi()
    emitted = json.dumps(schema["paths"]["/count"]["get"]["responses"]["200"])
    assert "components/schemas/int" not in emitted
