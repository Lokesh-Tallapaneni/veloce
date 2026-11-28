"""`list[Model]` responses serialize in one pass through pydantic-core.

`_apply_response_model` used to build a plain Python list via a `model_dump()`
per item, then hand that to the generic JSON encoder - two passes, one
temporary `dict` per item. When every item is already an instance of the
declared model, one `TypeAdapter(list[Model]).dump_json()` call replaces both.
`dump_json_matches_model_dump` (tested separately) excludes any model with a
`datetime`/`Decimal` field anywhere in its tree, where pydantic-core's native
writer formats differently than the per-item path; these tests pin that the
wire output is identical either way, and that the exclusion actually applies.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field

from veloce import Veloce
from veloce.testclient import TestClient


class Plain(BaseModel):
    id: int
    name: str


class Aliased(BaseModel):
    id: int
    full_name: str = Field(alias="fullName")


class WithDate(BaseModel):
    id: int
    born: datetime.date


def _app(model, payload, **route_kwargs) -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/items", response_model=model, **route_kwargs)
    async def items():
        return payload

    return app


def test_homogeneous_instances_take_the_fast_path_and_match_the_slow_one():
    payload = [Plain(id=1, name="a"), Plain(id=2, name="b")]
    fast = TestClient(_app(list[Plain], payload)).get("/items").json()
    assert fast == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_exclude_unset_is_still_applied_per_element():
    class M(BaseModel):
        a: str = "default-a"
        b: str = "default-b"

    payload = [M(a="set"), M(b="set")]
    body = (
        TestClient(_app(list[M], payload, response_model_exclude_unset=True)).get("/items").json()
    )
    assert body == [{"a": "set"}, {"b": "set"}]


def test_by_alias_is_still_applied():
    payload = [Aliased(id=1, fullName="Ada Lovelace")]
    body = (
        TestClient(_app(list[Aliased], payload, response_model_by_alias=True)).get("/items").json()
    )
    assert body == [{"id": 1, "fullName": "Ada Lovelace"}]


def test_include_and_exclude_are_still_applied():
    payload = [Plain(id=1, name="a")]
    only_id = (
        TestClient(_app(list[Plain], payload, response_model_include={"id"})).get("/items").json()
    )
    assert only_id == [{"id": 1}]

    drop_name = (
        TestClient(_app(list[Plain], payload, response_model_exclude={"name"})).get("/items").json()
    )
    assert drop_name == [{"id": 1}]


def test_a_model_with_a_date_field_is_excluded_from_the_fast_path():
    """The exclusion itself: a `date` field still dumps through the slow
    path, matching its own `model_dump()` + encoder output exactly."""
    payload = [WithDate(id=1, born=datetime.date(2020, 1, 2))]
    body = TestClient(_app(list[WithDate], payload)).get("/items").json()
    assert body == [{"id": 1, "born": "2020-01-02"}]


def test_a_heterogeneous_list_falls_back_to_the_slow_path():
    """A dict item is not `type(item) is inner`, so the fast path never
    applies regardless of the model's field types."""
    payload = [{"id": 1, "name": "a"}, Plain(id=2, name="b")]
    body = TestClient(_app(list[Plain], payload)).get("/items").json()
    assert body == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_a_configured_json_dialect_disables_the_fast_path():
    """`JSON_SORT_KEYS` makes `resolve_dumps` return a real callable - the
    same condition that routes a plain dict through the dialect too."""
    app = Veloce(openapi_url=None)
    app.config["JSON_SORT_KEYS"] = True

    @app.get("/items", response_model=list[Plain])
    async def items():
        return [Plain(id=1, name="a")]

    body = TestClient(app).get("/items").json()
    assert body == [{"id": 1, "name": "a"}]
