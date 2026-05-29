"""Schema emission for parametrised `dict[K, V]` annotations."""

from veloce.contrib.openapi import _python_type_to_schema


def test_dict_str_int_emits_typed_additional_properties() -> None:
    assert _python_type_to_schema(dict[str, int]) == {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    }


def test_dict_str_str_emits_typed_additional_properties() -> None:
    assert _python_type_to_schema(dict[str, str]) == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }


def test_nested_dict_recurses_into_value_schema() -> None:
    assert _python_type_to_schema(dict[str, dict[str, int]]) == {
        "type": "object",
        "additionalProperties": {
            "type": "object",
            "additionalProperties": {"type": "integer"},
        },
    }


def test_bare_dict_has_no_additional_properties_constraint() -> None:
    schema = _python_type_to_schema(dict)
    assert schema == {"type": "object"}
    assert "additionalProperties" not in schema
