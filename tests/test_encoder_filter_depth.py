"""Every `jsonable_encoder` filter applies at every depth.

The filters were forwarded into the recursion by hand at five sites, and the
copies had drifted in two ways:

* the `_public_vars` fallback omitted `exclude_none`, so an arbitrary object
  kept its `None` attributes while a plain dict dropped them;
* no site forwarded `exclude_unset` / `exclude_defaults`, so those worked on a
  model passed in directly and were silently ignored one level down.

Both are depth-dependence, which is the worst shape for a filter: the caller
gets the behaviour they asked for or not depending on how deeply the model
happens to be nested. These tests state the property once - the filter's effect
does not depend on depth or on container shape - and check it across the
product of both, so a site that stops forwarding fails here.

The other two thirds of `veloce/encoders.py` are in
`test_jsonable_encoder.py` (what each type coerces to) and
`test_encoder_registry.py` (dispatch and the registry).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from veloce.encoders import jsonable_encoder


class Model(BaseModel):
    set_field: int = 1
    default_field: int = 2


class Plain:
    """An arbitrary object, which reaches the `_public_vars` fallback."""

    def __init__(self) -> None:
        self.name = "x"
        self.missing = None


def _nest(value, shape: str):
    """Wrap `value` in each container shape the encoder recurses through."""
    return {
        "bare": value,
        "dict": {"k": value},
        "list": [value],
        "tuple": (value,),
        "deep": {"a": [{"b": value}]},
    }[shape]


def _unwrap(encoded, shape: str):
    return {
        "bare": lambda e: e,
        "dict": lambda e: e["k"],
        "list": lambda e: e[0],
        "tuple": lambda e: e[0],
        "deep": lambda e: e["a"][0]["b"],
    }[shape](encoded)


SHAPES = ["bare", "dict", "list", "tuple", "deep"]


# ── exclude_unset reaches a nested model ─────────────────────────────


@pytest.mark.parametrize("shape", SHAPES)
def test_exclude_unset_applies_at_every_depth(shape):
    """The defect: this worked for `bare` and was ignored everywhere else."""
    encoded = jsonable_encoder(_nest(Model(set_field=5), shape), exclude_unset=True)
    assert _unwrap(encoded, shape) == {"set_field": 5}


@pytest.mark.parametrize("shape", SHAPES)
def test_exclude_defaults_applies_at_every_depth(shape):
    encoded = jsonable_encoder(_nest(Model(set_field=5), shape), exclude_defaults=True)
    assert _unwrap(encoded, shape) == {"set_field": 5}


@pytest.mark.parametrize("shape", SHAPES)
def test_exclude_none_applies_at_every_depth(shape):
    encoded = jsonable_encoder(_nest({"name": "x", "missing": None}, shape), exclude_none=True)
    assert _unwrap(encoded, shape) == {"name": "x"}


@pytest.mark.parametrize("shape", SHAPES)
def test_exclude_applies_at_every_depth(shape):
    """The one the docstring already promised; kept so it cannot regress."""
    encoded = jsonable_encoder(_nest({"name": "x", "password": "s"}, shape), exclude={"password"})
    assert _unwrap(encoded, shape) == {"name": "x"}


@pytest.mark.parametrize("shape", SHAPES)
def test_include_applies_at_every_depth(shape):
    """`include` is a whitelist over keys at *all* depths, so a nesting key must
    be listed too or the branch holding the value is dropped - which is the
    documented behaviour, not depth-dependence."""
    keys = {"name", "k", "a", "b"}
    encoded = jsonable_encoder(_nest({"name": "x", "password": "s"}, shape), include=keys)
    assert _unwrap(encoded, shape) == {"name": "x"}


def test_include_drops_an_unlisted_nesting_key():
    """The other half of that contract, stated so it is not mistaken for a bug."""
    assert jsonable_encoder({"k": {"name": "x"}}, include={"name"}) == {}


# ── the arbitrary-object fallback honours them too ───────────────────


def test_an_arbitrary_object_honours_exclude_none():
    """The `_public_vars` fallback omitted this while every sibling had it."""
    assert jsonable_encoder(Plain(), exclude_none=True) == {"name": "x"}


@pytest.mark.parametrize("shape", SHAPES)
def test_an_arbitrary_object_honours_exclude_none_at_depth(shape):
    encoded = jsonable_encoder(_nest(Plain(), shape), exclude_none=True)
    assert _unwrap(encoded, shape) == {"name": "x"}


def test_an_arbitrary_object_honours_exclude():
    assert jsonable_encoder(Plain(), exclude={"missing"}) == {"name": "x"}


# ── the property, stated directly ────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "kwargs"),
    [
        (Model(set_field=5), {"exclude_unset": True}),
        (Model(set_field=5), {"exclude_defaults": True}),
        ({"name": "x", "missing": None}, {"exclude_none": True}),
        ({"name": "x", "password": "s"}, {"exclude": {"password"}}),
    ],
    ids=["unset", "defaults", "none", "exclude"],
)
def test_a_filters_effect_does_not_depend_on_depth(value, kwargs):
    """Whatever a filter does to a bare value, it does at any nesting.

    `include` is excluded from this product deliberately: it whitelists keys
    globally, so nesting keys change its input rather than its behaviour. It
    has its own test above.
    """
    bare = jsonable_encoder(value, **kwargs)
    for shape in SHAPES:
        encoded = jsonable_encoder(_nest(value, shape), **kwargs)
        assert _unwrap(encoded, shape) == bare, shape


def test_a_model_field_holding_a_dict_is_filtered_too():
    """Re-encoding a model applies the filter to a plain-dict field as well.

    The shapes above nest values in plain containers; a dict reached as a model
    *field* goes through the model re-encoding branch instead, which is where
    the forwarding was missing.
    """

    class Wrapper(BaseModel):
        top: str | None = None
        meta: dict[str, Any]

    result = jsonable_encoder(Wrapper(top=None, meta={"x": None, "y": 1}), exclude_none=True)
    assert "top" not in result
    assert result["meta"] == {"y": 1}


# ── the negatives: no filter means no filtering ──────────────────────


@pytest.mark.parametrize("shape", SHAPES)
def test_no_filter_keeps_every_field(shape):
    """A forwarding bug that dropped fields unconditionally would pass the
    assertions above and fail here."""
    encoded = jsonable_encoder(_nest(Model(set_field=5), shape))
    assert _unwrap(encoded, shape) == {"set_field": 5, "default_field": 2}


@pytest.mark.parametrize("shape", SHAPES)
def test_no_filter_keeps_none_values(shape):
    encoded = jsonable_encoder(_nest({"name": "x", "missing": None}, shape))
    assert _unwrap(encoded, shape) == {"name": "x", "missing": None}


def test_an_unset_field_that_was_set_survives():
    """`exclude_unset` must drop only what was genuinely never assigned."""
    encoded = jsonable_encoder({"m": Model(set_field=5, default_field=2)}, exclude_unset=True)
    assert encoded["m"] == {"set_field": 5, "default_field": 2}


def test_exclude_defaults_keeps_a_non_default_value():
    encoded = jsonable_encoder({"m": Model(set_field=5, default_field=9)}, exclude_defaults=True)
    assert encoded["m"] == {"set_field": 5, "default_field": 9}


# ── `include` reaching a model, which is where it reads as a bug ──────
#
# The plain-dict contract is stated above. The same rule reached through a
# *model* is what surprises: `include={"a"}` on a model with a nested model
# field yields `{"a": {}}`, because the nested keys are not on the whitelist.
# Deliberate, and pinned here so the surprising half has a test of its own -
# it was described in a CHANGELOG `### Fixed` entry as being "as documented"
# when the docstring said nothing about `include` at all.


class NestedInner(BaseModel):
    b: int = 1
    c: int = 2


class NestedOuter(BaseModel):
    a: NestedInner = NestedInner()
    d: int = 9


def test_include_on_a_model_field_empties_it_unless_its_keys_are_listed():
    """The report's exact shape, made a decision rather than a discovery."""
    assert jsonable_encoder(NestedOuter(), include={"a"}) == {"a": {}}


def test_listing_the_nested_keys_keeps_them():
    """The other half: the whitelist is over key names, so name them."""
    assert jsonable_encoder(NestedOuter(), include={"a", "b", "c"}) == {"a": {"b": 1, "c": 2}}


def test_exclude_on_a_model_needs_no_such_listing():
    """`exclude` is the blacklist reading of the same rule, and is unaffected."""
    assert jsonable_encoder(NestedOuter(), exclude={"d"}) == {"a": {"b": 1, "c": 2}}


def test_the_docstring_states_the_include_contract():
    """The CHANGELOG called this documented; that is now true.

    A corpus check rather than a behaviour one: the surprise is only a surprise
    because the public docstring described every other filter's depth behaviour
    and omitted this one.
    """
    doc = jsonable_encoder.__doc__ or ""

    assert "include" in doc
    assert "whitelist" in doc
