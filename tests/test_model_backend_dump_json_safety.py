"""`dump_json_matches_model_dump` — gating the `list[Model]` fast path.

pydantic-core's native `TypeAdapter.dump_json` diverges from `model_dump()`
run through the app's own JSON encoder on several types: a UTC `datetime`
(`Z` suffix vs `+00:00`), a `Decimal` or `timedelta` (a string vs a number),
`bytes` (the raw string vs base64), and a `set`/`frozenset` (its own hash
order vs sorted). A field typed `Any`, or a model accepting `extra="allow"`
fields, can hold any of these at runtime with no declared type to check. Any
of this anywhere in a model's field tree - directly, nested, or through
`list`/`dict`/`X | None` - must not take the fast path.
"""

from __future__ import annotations

import datetime
import decimal
import typing
import uuid

from pydantic import BaseModel, ConfigDict

from veloce._model_backend import dump_json_matches_model_dump


class Plain(BaseModel):
    name: str
    age: int


def test_a_model_with_only_plain_fields_is_safe():
    assert dump_json_matches_model_dump(Plain) is True


class WithDate(BaseModel):
    name: str
    dob: datetime.date


def test_a_date_field_is_unsafe():
    assert dump_json_matches_model_dump(WithDate) is False


class WithDatetime(BaseModel):
    at: datetime.datetime


def test_a_datetime_field_is_unsafe():
    assert dump_json_matches_model_dump(WithDatetime) is False


class WithDecimal(BaseModel):
    price: decimal.Decimal


def test_a_decimal_field_is_unsafe():
    assert dump_json_matches_model_dump(WithDecimal) is False


class WithTimedelta(BaseModel):
    dur: datetime.timedelta


def test_a_timedelta_field_is_unsafe():
    assert dump_json_matches_model_dump(WithTimedelta) is False


class WithBytes(BaseModel):
    data: bytes


def test_a_bytes_field_is_unsafe():
    assert dump_json_matches_model_dump(WithBytes) is False


class WithBareSet(BaseModel):
    tags: set


def test_a_bare_set_field_is_unsafe():
    assert dump_json_matches_model_dump(WithBareSet) is False


class WithParameterizedSet(BaseModel):
    tags: set[str]


def test_a_parameterized_set_field_is_unsafe():
    assert dump_json_matches_model_dump(WithParameterizedSet) is False


class WithFrozenset(BaseModel):
    tags: frozenset[str]


def test_a_frozenset_field_is_unsafe():
    assert dump_json_matches_model_dump(WithFrozenset) is False


class WithAny(BaseModel):
    value: typing.Any


def test_an_any_typed_field_is_unsafe():
    assert dump_json_matches_model_dump(WithAny) is False


class WithExtraAllowed(BaseModel):
    model_config = ConfigDict(extra="allow")

    x: int


def test_a_model_accepting_extra_fields_is_unsafe():
    assert dump_json_matches_model_dump(WithExtraAllowed) is False


class Nested(BaseModel):
    inner: WithDate


def test_an_unsafe_type_nested_in_another_model_is_unsafe():
    assert dump_json_matches_model_dump(Nested) is False


class ListOfUnsafe(BaseModel):
    items: list[WithDate]


def test_an_unsafe_type_inside_a_list_is_unsafe():
    assert dump_json_matches_model_dump(ListOfUnsafe) is False


class DictOfUnsafe(BaseModel):
    items: dict[str, decimal.Decimal]


def test_an_unsafe_type_inside_a_dict_is_unsafe():
    assert dump_json_matches_model_dump(DictOfUnsafe) is False


class OptionalUnsafe(BaseModel):
    price: decimal.Decimal | None


def test_an_unsafe_type_behind_optional_is_unsafe():
    assert dump_json_matches_model_dump(OptionalUnsafe) is False


class Safe(BaseModel):
    id: uuid.UUID
    tags: list[str]
    meta: dict[str, int] | None
    nested: Plain


def test_uuid_list_dict_and_a_safe_nested_model_are_all_safe():
    assert dump_json_matches_model_dump(Safe) is True


class SelfReferential(BaseModel):
    name: str
    child: SelfReferential | None = None


def test_a_self_referential_safe_model_terminates():
    assert dump_json_matches_model_dump(SelfReferential) is True


def test_the_result_is_cached_and_stable_across_calls():
    first = dump_json_matches_model_dump(Plain)
    second = dump_json_matches_model_dump(Plain)
    assert first is second is True
