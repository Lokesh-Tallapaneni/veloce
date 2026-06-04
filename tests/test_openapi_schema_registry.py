"""Collision-aware, identity-keyed schema registry and dual-mode schemas.

Covers the OpenAPI generation findings:
- Two distinct models sharing a ``__name__`` no longer collide (each keeps a
  distinct, module-qualified component name with correct ``$ref`` targets).
- Nested-model references resolve to ``#/components/schemas/...`` rather than
  Pydantic's raw ``#/$defs/...`` form.
- Request bodies use the validation schema, response models the serialization
  schema; the two collapse to one component when byte-identical and split into
  ``Name`` / ``Name-Output`` only on real divergence.
"""

from __future__ import annotations

import sys
import types

from pydantic import BaseModel, computed_field

from veloce import Veloce


def _make_named_model(cls_name: str, module: str, fields: dict[str, type]) -> type[BaseModel]:
    """Build a fresh BaseModel subclass with an explicit name and module."""
    if module not in sys.modules:
        sys.modules[module] = types.ModuleType(module)
    model = type(cls_name, (BaseModel,), {"__annotations__": dict(fields)})
    model.__module__ = module
    model.__qualname__ = cls_name
    return model


# Module-level models so `get_type_hints` resolves handler body annotations
# (a class defined inside a test function is not visible to the resolver).
class _Inner(BaseModel):
    x: int


class _Outer(BaseModel):
    inner: _Inner


class _Plain(BaseModel):
    a: int


class _Account(BaseModel):
    balance: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def doubled(self) -> int:
        return self.balance * 2


# Two distinct classes that deliberately share the same ``__name__`` ("User")
# but live in different modules, to exercise collision-aware naming. Defined at
# module scope (and bound to module-global names) so `get_type_hints` resolves
# the string annotations under `from __future__ import annotations`.
_UserSchemas = _make_named_model("User", "myapp.schemas", {"name": str})
_UserDb = _make_named_model("User", "myapp.db", {"id": int})
_UniqueItem = _make_named_model("UniqueItem", "myapp.solo", {"value": int})


def test_same_name_models_do_not_collide() -> None:
    app = Veloce()

    @app.post("/a", name="route_a")
    async def a(request, body: _UserSchemas):
        return {}

    @app.post("/b", name="route_b")
    async def b(request, body: _UserDb):
        return {}

    schema = app.openapi()
    names = set(schema["components"]["schemas"])
    assert names == {"User__schemas", "User__db"}

    ref_a = schema["paths"]["/a"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    ref_b = schema["paths"]["/b"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert ref_a["$ref"] == "#/components/schemas/User__schemas"
    assert ref_b["$ref"] == "#/components/schemas/User__db"
    # The two components carry their own (distinct) field sets.
    assert "name" in schema["components"]["schemas"]["User__schemas"]["properties"]
    assert "id" in schema["components"]["schemas"]["User__db"]["properties"]


def test_unique_name_keeps_bare_component_name() -> None:
    app = Veloce()

    @app.post("/x")
    async def x(request, body: _UniqueItem):
        return {}

    schema = app.openapi()
    assert "UniqueItem" in schema["components"]["schemas"]
    ref = schema["paths"]["/x"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert ref["$ref"] == "#/components/schemas/UniqueItem"


def test_nested_model_refs_point_at_components() -> None:
    app = Veloce()

    @app.post("/o")
    async def o(request, body: _Outer):
        return {}

    schema = app.openapi()
    # The nested ref must be rewritten from Pydantic's `#/$defs/_Inner` form.
    outer = schema["components"]["schemas"]["_Outer"]
    assert outer["properties"]["inner"]["$ref"] == "#/components/schemas/_Inner"
    assert "_Inner" in schema["components"]["schemas"]
    # No leftover $defs or placeholder refs anywhere in the document.
    blob = repr(schema)
    assert "$defs" not in blob
    assert "$veloce-schema" not in blob


def test_validation_and_serialization_collapse_when_identical() -> None:
    app = Veloce()

    @app.post("/p", response_model=_Plain)
    async def p(request, body: _Plain):
        return {}

    schema = app.openapi()
    # Same shape for input and output -> one component, no `-Output` twin.
    assert list(schema["components"]["schemas"]) == ["_Plain"]
    op = schema["paths"]["/p"]["post"]
    assert (
        op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/_Plain"
    )
    assert (
        op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/_Plain"
    )


def test_diverging_model_splits_into_output_variant() -> None:
    app = Veloce()

    @app.post("/acct", response_model=_Account)
    async def acct(request, body: _Account):
        return {}

    schema = app.openapi()
    names = set(schema["components"]["schemas"])
    assert names == {"_Account", "_Account-Output"}
    op = schema["paths"]["/acct"]["post"]
    # Request -> validation schema (no computed field).
    assert (
        op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/_Account"
    )
    assert "doubled" not in schema["components"]["schemas"]["_Account"]["properties"]
    # Response -> serialization schema (computed field present).
    assert (
        op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/_Account-Output"
    )
    assert "doubled" in schema["components"]["schemas"]["_Account-Output"]["properties"]


def test_separate_input_output_flag_disables_split() -> None:
    app = Veloce(separate_input_output_schemas=False)

    @app.post("/acct", response_model=_Account)
    async def acct(request, body: _Account):
        return {}

    schema = app.openapi()
    # Split disabled -> response reuses the validation schema, one component.
    assert list(schema["components"]["schemas"]) == ["_Account"]
    op = schema["paths"]["/acct"]["post"]
    assert (
        op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/_Account"
    )
