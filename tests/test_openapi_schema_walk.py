"""One walk over a generated schema, and it is not bounded by the stack.

`contrib/openapi.py` visited a generated schema five separate times, each with
its own recursive function differing only in what it did at each mapping:
rewrite a `binary` format, collect `#/$defs/` targets, repoint renamed refs,
resolve placeholder refs, and validate that every ref resolves.

They now share `_iter_dicts`, which is **iterative**. That is not incidental: a
recursive walk over a deeply nested schema is bounded by the interpreter's
recursion limit rather than by memory, and a model that refers to itself through
a long chain generates exactly that shape.

    depth = sys.getrecursionlimit() * 3

    recursive form -> RecursionError: maximum recursion depth exceeded
    iterative form -> completes

These tests pin both halves: that every pass still reaches every mapping, and
that the depth a schema can reach is no longer the recursion limit.
"""

from __future__ import annotations

import sys

import pytest
from pydantic import BaseModel

from veloce.contrib.openapi import _iter_dicts, _local_def_refs, _rewrite_byte_format


class TreeNode(BaseModel):
    """A self-referring model - the shape that generates a deeply nested schema.

    Module scope so `get_type_hints` can resolve the forward reference under
    this module's `from __future__ import annotations`.
    """

    value: int = 0
    children: list[TreeNode] = []


def _nest(leaf: dict, depth: int) -> dict:
    node = leaf
    for _ in range(depth):
        node = {"properties": {"inner": node}}
    return node


def _descend(node: dict, depth: int) -> dict:
    for _ in range(depth):
        node = node["properties"]["inner"]
    return node


# ── the walk reaches everything ──────────────────────────────────────


def test_every_mapping_is_yielded():
    # root, a, b, c, the two list entries, and the e/f inside them.
    tree = {"a": {"b": {"c": {}}}, "d": [{"e": {}}, {"f": {}}]}
    assert sum(1 for _ in _iter_dicts(tree)) == 8


def test_a_mapping_inside_a_list_is_yielded():
    assert any(m.get("marker") for m in _iter_dicts({"items": [{"marker": True}]}))


def test_a_mapping_inside_a_nested_list_is_yielded():
    assert any(m.get("marker") for m in _iter_dicts({"a": [[{"marker": True}]]}))


def test_the_root_mapping_is_yielded():
    root = {"root": True}
    assert root in list(_iter_dicts(root))


def test_a_scalar_yields_nothing():
    assert list(_iter_dicts("string")) == []
    assert list(_iter_dicts(7)) == []
    assert list(_iter_dicts(None)) == []


def test_an_empty_mapping_is_still_yielded():
    assert len(list(_iter_dicts({}))) == 1


def test_a_list_of_scalars_yields_nothing():
    assert list(_iter_dicts([1, "two", None])) == []


# ── each pass still does its job ─────────────────────────────────────


def test_a_binary_format_is_rewritten_at_the_root():
    node = {"type": "string", "format": "binary"}
    _rewrite_byte_format(node)
    assert node["format"] == "byte"


def test_a_binary_format_is_rewritten_when_nested():
    node = {"properties": {"f": {"type": "string", "format": "binary"}}}
    _rewrite_byte_format(node)
    assert node["properties"]["f"]["format"] == "byte"


def test_a_binary_format_is_rewritten_inside_a_list():
    node = {"anyOf": [{"type": "string", "format": "binary"}]}
    _rewrite_byte_format(node)
    assert node["anyOf"][0]["format"] == "byte"


def test_a_non_binary_format_is_left_alone():
    node = {"type": "string", "format": "date-time"}
    _rewrite_byte_format(node)
    assert node["format"] == "date-time"


def test_a_binary_format_on_a_non_string_is_left_alone():
    """Both conditions must hold, as before."""
    node = {"type": "integer", "format": "binary"}
    _rewrite_byte_format(node)
    assert node["format"] == "binary"


def test_local_def_refs_collects_from_every_depth():
    tree = {
        "$ref": "#/$defs/A",
        "properties": {"x": {"$ref": "#/$defs/B"}},
        "anyOf": [{"$ref": "#/$defs/C"}],
    }
    assert _local_def_refs(tree) == {"A", "B", "C"}


def test_local_def_refs_ignores_other_refs():
    tree = {"$ref": "#/components/schemas/Elsewhere"}
    assert _local_def_refs(tree) == set()


def test_local_def_refs_ignores_a_non_string_ref():
    assert _local_def_refs({"$ref": 7}) == set()


def test_local_def_refs_on_an_empty_tree():
    assert _local_def_refs({}) == set()


# ── and the depth is no longer the recursion limit ───────────────────


@pytest.mark.parametrize("multiple", [2, 3])
def test_a_schema_deeper_than_the_recursion_limit_is_walked(multiple):
    """The recursive form raised `RecursionError` at this depth."""
    depth = sys.getrecursionlimit() * multiple
    node = _nest({"type": "string", "format": "binary"}, depth)
    _rewrite_byte_format(node)
    assert _descend(node, depth)["format"] == "byte"


def test_a_deep_schema_is_fully_collected():
    depth = sys.getrecursionlimit() * 2
    node = _nest({"$ref": "#/$defs/Deep"}, depth)
    assert _local_def_refs(node) == {"Deep"}


def test_the_walk_itself_does_not_recurse():
    """Stated directly: no stack frame per level."""
    depth = sys.getrecursionlimit() * 4
    node = _nest({"leaf": True}, depth)
    assert sum(1 for _ in _iter_dicts(node)) == depth * 2 + 1


# ── the generated document is unchanged ──────────────────────────────


def test_a_document_with_nested_models_still_resolves_its_refs():
    from pydantic import BaseModel

    from veloce import Veloce

    class Inner(BaseModel):
        value: int

    class Outer(BaseModel):
        inner: Inner

    app = Veloce(title="T", version="1")

    @app.post("/o")
    async def route(payload: Outer) -> Outer:
        return payload

    schema = app.openapi()
    refs = {m["$ref"] for m in _iter_dicts(schema) if isinstance(m.get("$ref"), str)}
    known = {f"#/components/schemas/{n}" for n in schema["components"]["schemas"]}
    assert refs <= known, refs - known


def test_a_recursive_model_still_builds():
    """The shape that makes the recursive walk a real risk, not a theoretical one."""
    from veloce import Veloce

    app = Veloce(title="T", version="1")

    @app.post("/n")
    async def route(payload: TreeNode) -> TreeNode:
        return payload

    assert "TreeNode" in app.openapi()["components"]["schemas"]
