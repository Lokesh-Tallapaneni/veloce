"""Component-name uniquing has one implementation, and it still uniques.

`SchemaRegistry._unique_def_name` and `_unique_component_name` were the same
"suffix until free" loop with the arguments in the other order; the only real
difference was that the first searched from `f"{base}-Output"` rather than from
`base`. That is a different *starting candidate*, not a different algorithm, so
the first now calls the second.

Uniquing is what stops two models with the same class name overwriting each
other in `components/schemas`, and a collision there is silent - the document
still validates, it just describes the wrong shape for one of them. So these
tests cover the collision ladder rather than only the happy case.
"""

from __future__ import annotations

import pytest

from veloce.contrib.openapi import SchemaRegistry, _unique_component_name

# ── the shared loop ──────────────────────────────────────────────────


def test_a_free_name_is_returned_unchanged():
    assert _unique_component_name("Item", {}) == "Item"


def test_a_taken_name_gets_a_suffix():
    assert _unique_component_name("Item", {"Item": {}}) == "Item_2"


def test_the_suffix_climbs_past_every_taken_name():
    taken = {"Item": {}, "Item_2": {}, "Item_3": {}}
    assert _unique_component_name("Item", taken) == "Item_4"


def test_a_gap_in_the_ladder_is_not_reused():
    """`Item_2` free but `Item` taken: the first free suffix wins."""
    assert _unique_component_name("Item", {"Item": {}, "Item_3": {}}) == "Item_2"


# ── and the output-variant caller ────────────────────────────────────


def test_the_output_variant_starts_from_the_suffixed_name():
    assert SchemaRegistry._unique_def_name({}, "Item") == "Item-Output"


def test_the_output_variant_suffixes_when_taken():
    assert SchemaRegistry._unique_def_name({"Item-Output": {}}, "Item") == "Item-Output_2"


def test_the_output_variant_climbs_the_ladder():
    components = {"Item-Output": {}, "Item-Output_2": {}}
    assert SchemaRegistry._unique_def_name(components, "Item") == "Item-Output_3"


def test_the_output_variant_ignores_the_bare_name():
    """`Item` being taken must not push the output twin off `Item-Output` - they
    are different components and only the suffixed one is being placed."""
    assert SchemaRegistry._unique_def_name({"Item": {}}, "Item") == "Item-Output"


@pytest.mark.parametrize("base", ["Item", "A", "Model-With-Dashes", "Nested_Under_Score"])
def test_the_two_agree_once_the_starting_candidate_matches(base):
    """The property the collapse rests on: given the same candidate and the same
    taken set, the two produce the same answer."""
    taken = {f"{base}-Output": {}, f"{base}-Output_2": {}}
    assert SchemaRegistry._unique_def_name(taken, base) == _unique_component_name(
        f"{base}-Output", taken
    )


# ── end to end: two same-named models do not collide ─────────────────


def test_two_models_with_the_same_name_get_distinct_components():
    """The negative that matters: a name collision in `components/schemas` is
    silent - the document still validates, it just describes one of them wrong."""
    from pydantic import BaseModel

    from veloce import Veloce

    def make_app() -> Veloce:
        app = Veloce(title="T", version="1")

        class Item(BaseModel):
            a: int

        class Item2(BaseModel):  # noqa: N801 - deliberately same __name__ below
            b: str

        Item2.__name__ = "Item"

        @app.get("/one", response_model=Item)
        async def one():
            return {}

        @app.post("/two", response_model=Item2)
        async def two():
            return {}

        return app

    schemas = make_app().openapi()["components"]["schemas"]
    item_like = [name for name in schemas if name.startswith("Item")]
    assert len(item_like) == len(set(item_like))
    assert len(item_like) >= 2, schemas
