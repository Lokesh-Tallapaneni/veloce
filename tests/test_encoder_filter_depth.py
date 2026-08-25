"""`include` / `exclude` reach every depth, models included.

`jsonable_encoder`'s docstring says the filters "apply to dict keys at **every
depth** - passing `exclude={"password"}` strips a `password` key wherever it
appears in the structure, not only at the top level."

Plain dicts did that. Models did not: the `BaseModel` branch forwarded
`exclude_none` and `custom_encoder` and dropped `include`/`exclude`, so anything
below a model kept its secrets:

    dict        {'a': {'b': 1}}                                    stripped
    model       {'name':'n','meta':{'password':'nested','k':1}}    SURVIVED
    nestedmodel {'inner':{'password':'q','k':2}}                   SURVIVED

The obvious use - sanitising a payload before logging it - is exactly the one
that silently failed, and it failed on the half a caller is least likely to
inspect.

The dataclass branch immediately below the model branch already forwarded all
three, so models and dataclasses disagreed inside one function. They now agree,
and the behaviour matches what the docstring always claimed.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import BaseModel

from veloce.encoders import jsonable_encoder


class Inner(BaseModel):
    password: str
    k: int


class Outer(BaseModel):
    name: str
    meta: dict


class Nested(BaseModel):
    inner: Inner


class Deep(BaseModel):
    level: Nested


@dataclasses.dataclass
class InnerDC:
    password: str
    k: int


@dataclasses.dataclass
class OuterDC:
    name: str
    inner: InnerDC


# ── exclude reaches every depth ──────────────────────────────────────


def test_a_top_level_dict_key_is_excluded():
    assert jsonable_encoder({"password": "x", "a": 1}, exclude={"password"}) == {"a": 1}


def test_a_nested_dict_key_is_excluded():
    """This half always worked."""
    payload = {"a": {"password": "deep", "b": 1}}
    assert jsonable_encoder(payload, exclude={"password"}) == {"a": {"b": 1}}


def test_a_dict_under_a_model_is_excluded():
    """The defect: `meta` kept its `password`."""
    obj = Outer(name="n", meta={"password": "nested", "k": 1})
    assert jsonable_encoder(obj, exclude={"password"}) == {"name": "n", "meta": {"k": 1}}


def test_a_nested_model_field_is_excluded():
    """The defect: `inner` kept its `password`."""
    obj = Nested(inner=Inner(password="q", k=2))
    assert jsonable_encoder(obj, exclude={"password"}) == {"inner": {"k": 2}}


def test_two_levels_of_model_are_excluded():
    obj = Deep(level=Nested(inner=Inner(password="q", k=2)))
    assert jsonable_encoder(obj, exclude={"password"}) == {"level": {"inner": {"k": 2}}}


def test_a_model_inside_a_list_is_excluded():
    obj = [Inner(password="a", k=1), Inner(password="b", k=2)]
    assert jsonable_encoder(obj, exclude={"password"}) == [{"k": 1}, {"k": 2}]


def test_a_model_inside_a_dict_value_is_excluded():
    obj = {"first": Inner(password="a", k=1)}
    assert jsonable_encoder(obj, exclude={"password"}) == {"first": {"k": 1}}


def test_a_models_own_field_is_still_excluded():
    """`model_dump` already did this; forwarding must not undo it."""
    assert jsonable_encoder(Inner(password="q", k=2), exclude={"password"}) == {"k": 2}


def test_excluding_several_keys():
    obj = Nested(inner=Inner(password="q", k=2))
    assert jsonable_encoder(obj, exclude={"password", "k"}) == {"inner": {}}


def test_excluding_a_key_that_is_not_there_changes_nothing():
    obj = Nested(inner=Inner(password="q", k=2))
    assert jsonable_encoder(obj, exclude={"absent"}) == {"inner": {"password": "q", "k": 2}}


# ── models and dataclasses agree ─────────────────────────────────────


def test_a_dataclass_still_excludes_at_every_depth():
    """The branch that was already right."""
    obj = OuterDC(name="n", inner=InnerDC(password="q", k=2))
    assert jsonable_encoder(obj, exclude={"password"}) == {"name": "n", "inner": {"k": 2}}


def test_the_two_branches_agree():
    """The property: which kind of object it is must not decide this."""
    model = jsonable_encoder(Nested(inner=Inner(password="q", k=2)), exclude={"password"})
    dc = jsonable_encoder(InnerDC(password="q", k=2), exclude={"password"})
    assert model["inner"] == dc


# ── include reaches every depth too ──────────────────────────────────


def test_include_keeps_only_the_named_key():
    obj = Outer(name="n", meta={"k": 1})
    assert jsonable_encoder(obj, include={"name"}) == {"name": "n"}


def test_include_applies_under_a_model():
    obj = Nested(inner=Inner(password="q", k=2))
    assert jsonable_encoder(obj, include={"inner", "k"}) == {"inner": {"k": 2}}


# ── nothing changes when no filter is given ──────────────────────────


def test_no_filter_leaves_the_structure_whole():
    obj = Nested(inner=Inner(password="q", k=2))
    assert jsonable_encoder(obj) == {"inner": {"password": "q", "k": 2}}


def test_no_filter_on_a_deep_structure():
    obj = Deep(level=Nested(inner=Inner(password="q", k=2)))
    assert jsonable_encoder(obj) == {"level": {"inner": {"password": "q", "k": 2}}}


@pytest.mark.parametrize("payload", [{"a": 1}, [1, 2], "text", 5, None, True])
def test_a_plain_value_is_unchanged(payload):
    assert jsonable_encoder(payload) == payload


# ── the sibling filters still behave ─────────────────────────────────


def test_exclude_none_still_reaches_a_nested_dict():
    """The one filter that was already forwarded."""

    class WithNone(BaseModel):
        a: int
        meta: dict

    obj = WithNone(a=1, meta={"b": None, "c": 2})
    assert jsonable_encoder(obj, exclude_none=True) == {"a": 1, "meta": {"c": 2}}


def test_exclude_none_and_exclude_compose():
    class WithNone(BaseModel):
        a: int
        meta: dict

    obj = WithNone(a=1, meta={"b": None, "password": "q", "c": 2})
    assert jsonable_encoder(obj, exclude_none=True, exclude={"password"}) == {
        "a": 1,
        "meta": {"c": 2},
    }


def test_a_custom_encoder_still_applies_under_a_model():
    from decimal import Decimal

    class WithDecimal(BaseModel):
        amount: Decimal

    encoded = jsonable_encoder(
        WithDecimal(amount=Decimal("1.5")), custom_encoder={Decimal: lambda d: f"${d}"}
    )
    assert encoded == {"amount": "$1.5"}


def test_exclude_unset_still_works():
    class Partial(BaseModel):
        a: int = 1
        b: int = 2

    assert jsonable_encoder(Partial(a=5), exclude_unset=True) == {"a": 5}
