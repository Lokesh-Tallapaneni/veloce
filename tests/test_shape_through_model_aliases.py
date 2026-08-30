"""`shape_through_model` round-trips a model that declares field aliases.

The shaper dumps a Pydantic instance to a mapping before validating it, so that
a subclass returned under a base-model contract cannot leak the subclass's own
fields. The dump was by field name; a model with `Field(alias=...)` and the
default `populate_by_name=False` accepts only the alias, so it failed validating
its own dump.

That is not a corner: `MCPServer` catches the `ValidationError` and answers
`isError: true`, so every aliased-output MCP tool returned an error. The same
shaper serves `dispatch.py`'s list-of-model elements and the MCP task runner.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from veloce._model_backend import shape_through_model


class Aliased(BaseModel):
    """The default: the alias is the only accepted input name."""

    item_id: int = Field(alias="itemId")


class BothNames(BaseModel):
    """`populate_by_name=True` accepts either spelling."""

    model_config = ConfigDict(populate_by_name=True)

    item_id: int = Field(alias="itemId")


class Plain(BaseModel):
    item_id: int


class Base(BaseModel):
    kept: int


class Richer(Base):
    secret: str = "s"


def test_an_aliased_model_survives_its_own_round_trip():
    """The regression: the shaper fed its dump back to the model that made it."""
    assert shape_through_model(Aliased(itemId=7), Aliased) == {"item_id": 7}


def test_an_aliased_model_that_accepts_either_name_still_works():
    assert shape_through_model(BothNames(itemId=7), BothNames) == {"item_id": 7}


def test_a_model_with_no_alias_is_unaffected():
    assert shape_through_model(Plain(item_id=7), Plain) == {"item_id": 7}


def test_a_subclass_still_loses_the_fields_the_contract_excludes():
    """The reason the mapping round trip exists at all - it must survive the fix."""
    shaped = shape_through_model(Richer(kept=1), Base)

    assert shaped == {"kept": 1}
    assert "secret" not in shaped, "a richer object leaked a field the contract excludes"


def test_an_aliased_subclass_loses_them_too():
    """Both properties at once, which is where a by-alias dump could go wrong."""

    class AliasedBase(BaseModel):
        kept: int = Field(alias="keptField")

    class AliasedRicher(AliasedBase):
        secret: str = "s"

    shaped = shape_through_model(AliasedRicher(keptField=1), AliasedBase)

    assert shaped == {"kept": 1}
    assert "secret" not in shaped


def test_a_value_that_does_not_conform_still_raises():
    """The shaper's other job: refusing what the contract does not admit."""
    with pytest.raises(Exception):
        shape_through_model({"wrong": "shape"}, Aliased)
