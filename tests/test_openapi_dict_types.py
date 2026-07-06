"""Schema emission for parametrised `dict[K, V]` annotations.

`_python_type_to_schema` builds the schema for a non-body parameter / form
field only (body models go through `_pydantic_to_schema`). A non-body dict
parameter is not wire-addressable — the resolver only JSON-decodes a bare
model annotation, so `dict[str, int]` 422s on a JSON-object string and there
is no repeated-param form for a dict. The schema therefore documents a bare
object and never advertises typed `additionalProperties` the resolver would
reject.
"""

from veloce.contrib.openapi import _python_type_to_schema


def test_dict_str_int_emits_bare_object() -> None:
    assert _python_type_to_schema(dict[str, int]) == {"type": "object"}


def test_dict_str_str_emits_bare_object() -> None:
    assert _python_type_to_schema(dict[str, str]) == {"type": "object"}


def test_nested_dict_emits_bare_object() -> None:
    assert _python_type_to_schema(dict[str, dict[str, int]]) == {"type": "object"}


def test_bare_dict_has_no_additional_properties_constraint() -> None:
    schema = _python_type_to_schema(dict)
    assert schema == {"type": "object"}
    assert "additionalProperties" not in schema


def test_openapi_dict_schema_emits_bare_object():
    # `_python_type_to_schema` builds non-body parameter / form schemas only.
    # A dict parameter is not wire-addressable (the resolver 422s on a JSON
    # object string and there is no repeated-param form for a dict), so every
    # `dict[K, V]` documents a bare object rather than typed
    # `additionalProperties` the resolver would reject.
    from typing import Any

    assert _python_type_to_schema(dict[str, int]) == {"type": "object"}
    assert _python_type_to_schema(dict[str, str]) == {"type": "object"}
    assert _python_type_to_schema(dict[str, Any]) == {"type": "object"}

    schema_bare = _python_type_to_schema(dict)
    assert schema_bare == {"type": "object"}
    assert "additionalProperties" not in schema_bare
