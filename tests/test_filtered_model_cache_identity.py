"""The derived-model cache is keyed by the model, not by its address.

`_FILTERED_MODELS` keyed on `(id(model), include, exclude)` and held no
reference to the model. CPython reuses the address of a collected object, so a
model built after another was collected could land on the same `id` and be
handed the earlier model's derived schema - a route documented as returning
fields it has never heard of.

Reachable by any app that builds models dynamically and filters them: a
`create_model` per tenant or per API version, or simply repeated app
construction across a test session, combined with `response_model_include` /
`response_model_exclude`.

Holding the model weakly fixes both halves at once: the key is the object, so an
address collision means nothing, and the entry goes when the model does, so the
dict is no longer an unbounded process-global.
"""

from __future__ import annotations

import gc
import weakref

from pydantic import BaseModel, create_model

from veloce.contrib._jsonschema import _FILTERED_MODELS, _filtered_response_model


def _model(name: str, **fields):
    return create_model(name, **{key: (value, ...) for key, value in fields.items()})


def test_a_filtered_model_carries_only_the_surviving_fields():
    """The control: the derivation itself."""
    source = _model("M", keep=str, secret=str)

    derived = _filtered_response_model(source, {"keep"}, None)

    assert set(derived.model_fields) == {"keep"}


def test_the_same_model_and_filter_reuse_one_derived_model():
    """The cache's reason to exist: two routes filtering alike share a component."""
    source = _model("M", keep=str, secret=str)

    first = _filtered_response_model(source, {"keep"}, None)
    second = _filtered_response_model(source, {"keep"}, None)

    assert first is second


def test_two_filters_of_one_model_stay_distinct():
    source = _model("M", a=str, b=str, c=str)

    assert _filtered_response_model(source, {"a"}, None) is not _filtered_response_model(
        source, {"b"}, None
    )


def test_a_recycled_address_does_not_serve_another_models_schema():
    """The regression, driven until CPython actually reuses an address.

    Each iteration builds a model with a distinct field name, applies the *same*
    filter, and drops it. The filter has to be constant: the key is
    `(id, include, exclude)`, so varying the filter varies the key and no
    collision is possible however often an address is reused. With it constant,
    the address is the whole key, and CPython recycles one within two
    iterations - the second model is handed the first's derived class.
    """
    for i in range(200):
        source = _model(f"M{i}", **{f"field_{i}": str, "secret": str})

        derived = _filtered_response_model(source, None, {"secret"})

        assert set(derived.model_fields) == {f"field_{i}"}, (
            f"iteration {i} was served a derived model built for a different source"
        )
        del source, derived
        gc.collect()


def test_the_cache_releases_a_collected_models_entry():
    """The growth half, stated deterministically.

    The source was never pinned - it was the key's `id`, not the key - so what
    leaked was the derived model, one entry per filtered model for the process
    lifetime. Holding the source weakly is what lets the entry go with it.
    """
    source = _model("Transient", keep=str, secret=str)
    _filtered_response_model(source, {"keep"}, None)
    witness = weakref.ref(source)
    before = len(_FILTERED_MODELS)

    del source
    gc.collect()

    assert witness() is None
    assert len(_FILTERED_MODELS) < before, "the derived model outlived its source"


def test_a_struct_model_is_cached_the_same_way():
    """The msgspec half goes through the same cache."""
    msgspec = __import__("msgspec")

    class S(msgspec.Struct):
        keep: str
        secret: str

    first = _filtered_response_model(S, {"keep"}, None)
    second = _filtered_response_model(S, {"keep"}, None)

    assert first is second
    assert first.__struct_fields__ == ("keep",)


def test_an_unfiltered_model_is_returned_as_itself():
    """No filter, no derivation, nothing cached."""
    source = _model("M", a=str)

    assert _filtered_response_model(source, None, None) is source


def test_a_model_declared_normally_still_works():
    """`create_model` is the dynamic case; the ordinary one must be unaffected."""

    class Ordinary(BaseModel):
        keep: str
        secret: str

    derived = _filtered_response_model(Ordinary, None, {"secret"})

    assert set(derived.model_fields) == {"keep"}
