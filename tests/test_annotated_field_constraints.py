"""A pydantic `Field` inside `Annotated` — folded into the parameter marker.

`Annotated[int, Field(ge=1, le=10)]` is the spelling users bring from other
frameworks and it declares what `Query(ge=1, le=10)` declares, so it has to be
both published and enforced. It used to be dropped: the schema advertised a
bare integer and the resolver accepted any value at all.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from annotated_types import Gt, Le, Len
from pydantic import BaseModel, Field

from tests._openapi import document, parameter
from veloce import Depends, Query, Veloce
from veloce._handler_plan import build_plan
from veloce.contrib.mcp.plan_bridge import build_input_schema
from veloce.http.datastructures import UploadFile
from veloce.testclient import TestClient


def _schema(handler) -> dict:
    """One handler's input-schema properties, as the MCP door publishes them."""
    return build_input_schema(build_plan(handler), {})["properties"]


@pytest.fixture
def client() -> TestClient:
    """One app covering every constrained shape the tests below read from."""
    app = Veloce()

    @app.get("/level")
    async def level(level: Annotated[int, Field(ge=1, le=10)] = 5):
        return {"level": level}

    @app.get("/name")
    async def name(
        name: Annotated[str, Field(min_length=2, max_length=4, pattern=r"[a-z]+")] = "ab",
    ):
        return {"name": name}

    @app.get("/items/{item_id}")
    async def item(item_id: Annotated[int, Field(ge=1)]):
        return {"item_id": item_id}

    @app.get("/plain")
    async def plain(note: Annotated[str, "just a string"] = "none", bare: int = 2):
        return {"note": note, "bare": bare}

    @app.get("/marked")
    async def marked(page: int = Query(default=1, ge=1, le=9)):
        return {"page": page}

    @app.get("/opt")
    async def opt(level: Annotated[int, Field(le=10)] | None = None):
        return {"level": level}

    @app.get("/tags")
    async def tags(tag: Annotated[list[str], Field(max_length=3, min_length=1)] = None):
        return {"tags": tag}

    @app.get("/marked-tags")
    async def marked_tags(tag: Annotated[list[str], Query(max_length=3)] = None):
        return {"tags": tag}

    @app.get("/split")
    async def split(level: Annotated[int, Field(ge=1), Field(le=10)] = 5):
        return {"level": level}

    @app.get("/narrowed")
    async def narrowed(level: Annotated[int, Field(ge=1), Field(ge=5)] = 5):
        return {"level": level}

    @app.get("/bare-gt")
    async def bare_gt(n: Annotated[int, Gt(0)] = 5):
        return {"n": n}

    @app.get("/bare-interval")
    async def bare_interval(n: Annotated[int, Gt(0), Le(10)] = 5):
        return {"n": n}

    @app.get("/bare-len")
    async def bare_len(code: Annotated[str, Len(3, 5)] = "abc"):
        return {"code": code}

    @app.get("/bare-and-field")
    async def bare_and_field(n: Annotated[int, Gt(0), Field(gt=3)] = 5):
        return {"n": n}

    @app.get("/pattern")
    async def pattern(code: Annotated[str, Field(pattern=r"[a-z]+")] = "ab"):
        return {"code": code}

    @app.get("/anchored")
    async def anchored(code: Annotated[str, Field(pattern=r"^[a-z]+$")] = "ab"):
        return {"code": code}

    return TestClient(app)


# ── schema ──────────────────────────────────────────────────────────


def test_numeric_constraints_reach_the_schema():
    def handler(level: Annotated[int, Field(ge=1, le=10, description="1-10")] = 5): ...

    assert _schema(handler)["level"] == {
        "type": "integer",
        "description": "1-10",
        "minimum": 1,
        "maximum": 10,
        "default": 5,
    }


def test_field_schema_matches_the_equivalent_marker():
    def with_field(level: Annotated[int, Field(ge=1, le=10)] = 5): ...

    def with_marker(level: int = Query(default=5, ge=1, le=10)): ...

    assert _schema(with_field) == _schema(with_marker)


def test_exclusive_and_string_constraints_reach_the_schema():
    def handler(
        count: Annotated[int, Field(gt=0, lt=100, multiple_of=5)],
        name: Annotated[str, Field(min_length=2, max_length=4, pattern=r"[a-z]+")],
    ): ...

    props = _schema(handler)
    assert props["count"]["exclusiveMinimum"] == 0
    assert props["count"]["exclusiveMaximum"] == 100
    assert props["count"]["multipleOf"] == 5
    assert props["name"]["minLength"] == 2
    assert props["name"]["maxLength"] == 4
    assert props["name"]["pattern"] == "[a-z]+"


def test_title_reaches_the_schema():
    def handler(level: Annotated[int, Field(title="Level", description="how loud")] = 1): ...

    props = _schema(handler)
    assert props["level"]["title"] == "Level"
    assert props["level"]["description"] == "how loud"


def test_the_field_default_is_adopted_when_the_parameter_has_none():
    def handler(level: Annotated[int, Field(3, ge=1)]): ...

    assert _schema(handler)["level"]["default"] == 3


def test_the_field_default_wins_over_the_parameter_default():
    def handler(level: Annotated[int, Field(3, ge=1)] = 5): ...

    assert _schema(handler)["level"]["default"] == 3


def test_a_field_default_factory_is_carried_onto_the_marker():
    def handler(tag: Annotated[str, Field(default_factory=lambda: "fresh")]): ...

    slot = build_plan(handler).slots[0]
    assert slot.default_factory is not None
    assert slot.default_factory() == "fresh"


def test_constraints_reach_the_openapi_document(client: TestClient):
    schema = document(client)
    level = parameter(schema, "/level", "level")
    assert level["schema"]["minimum"] == 1
    assert level["schema"]["maximum"] == 10
    assert parameter(schema, "/name", "name")["schema"]["pattern"] == "[a-z]+"


def test_a_path_segment_stays_a_path_parameter(client: TestClient):
    item_id = parameter(document(client), "/items/{item_id}", "item_id")
    assert item_id["in"] == "path"
    assert item_id["schema"]["minimum"] == 1


# ── enforcement: accepted ───────────────────────────────────────────


def test_an_in_range_value_is_accepted(client: TestClient):
    response = client.get("/level?level=7")
    assert response.status_code == 200
    assert response.json() == {"level": 7}


def test_the_boundary_values_are_accepted(client: TestClient):
    assert client.get("/level?level=1").json() == {"level": 1}
    assert client.get("/level?level=10").json() == {"level": 10}


def test_the_parameter_default_applies_when_the_value_is_absent(client: TestClient):
    response = client.get("/level")
    assert response.status_code == 200
    assert response.json() == {"level": 5}


def test_a_constrained_path_parameter_is_read_from_the_path(client: TestClient):
    response = client.get("/items/5")
    assert response.status_code == 200
    assert response.json() == {"item_id": 5}


def test_an_in_range_string_is_accepted(client: TestClient):
    assert client.get("/name?name=abc").json() == {"name": "abc"}


# ── enforcement: rejected ───────────────────────────────────────────


def test_a_value_above_the_maximum_is_rejected(client: TestClient):
    assert client.get("/level?level=999").status_code == 422


def test_a_value_below_the_minimum_is_rejected(client: TestClient):
    assert client.get("/level?level=-4").status_code == 422


def test_a_too_short_string_is_rejected(client: TestClient):
    assert client.get("/name?name=a").status_code == 422


def test_a_too_long_string_is_rejected(client: TestClient):
    assert client.get("/name?name=abcdef").status_code == 422


def test_a_pattern_mismatch_is_rejected(client: TestClient):
    assert client.get("/name?name=AB").status_code == 422


def test_a_constrained_path_parameter_out_of_range_is_rejected(client: TestClient):
    assert client.get("/items/0").status_code == 422


def test_an_optional_annotation_keeps_its_constraints(client: TestClient):
    assert client.get("/opt").json() == {"level": None}
    assert client.get("/opt?level=4").json() == {"level": 4}
    assert client.get("/opt?level=40").status_code == 422


# ── a Veloce marker wins ────────────────────────────────────────────


def test_a_marker_beside_a_field_wins_and_warns():
    def handler(level: Annotated[int, Query(ge=1, le=3), Field(ge=100)] = 2): ...

    with pytest.warns(UserWarning, match="level.*Query"):
        props = _schema(handler)
    assert props["level"]["minimum"] == 1
    assert props["level"]["maximum"] == 3


def test_a_marker_in_the_default_position_beside_a_field_wins_and_warns():
    def handler(level: Annotated[int, Field(ge=100)] = Query(default=2, le=3)): ...

    with pytest.warns(UserWarning, match="level.*Query"):
        props = _schema(handler)
    assert props["level"]["maximum"] == 3
    assert "minimum" not in props["level"]


def _seven() -> int:
    return 7


def test_a_depends_beside_a_field_warns():
    def handler(value: Annotated[int, Depends(_seven), Field(ge=100)]): ...

    with pytest.warns(UserWarning, match="value.*Depends"):
        build_plan(handler)


# ── everything else is unchanged ────────────────────────────────────


def test_non_field_metadata_is_untouched(client: TestClient):
    response = client.get("/plain?note=hi")
    assert response.status_code == 200
    assert response.json() == {"note": "hi", "bare": 2}


def test_a_plain_annotated_parameter_builds_the_same_slot_as_before():
    def handler(note: Annotated[str, "just a string"] = "none"): ...

    assert build_plan(handler).slots[0].marker is None
    assert _schema(handler)["note"] == {"type": "string", "default": "none"}


def test_an_unannotated_parameter_is_unchanged(client: TestClient):
    assert client.get("/plain?bare=9").json() == {"note": "none", "bare": 9}


def test_a_marker_only_parameter_is_unchanged(client: TestClient):
    assert client.get("/marked?page=4").json() == {"page": 4}
    assert client.get("/marked").json() == {"page": 1}
    assert client.get("/marked?page=40").status_code == 422


# ── list-typed parameters ───────────────────────────────────────────


def test_list_length_bounds_reach_the_schema():
    def handler(tag: Annotated[list[str], Field(min_length=1, max_length=3)] = None): ...

    # No `"default": None`: the published type is a non-nullable array, and a
    # schema whose own default violates its declared type is one a strict client
    # rejects and an agent may echo back as `null` for the door to refuse.
    assert _schema(handler)["tag"] == {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "maxItems": 3,
    }


def test_element_constraints_land_on_the_items_subschema():
    def handler(n: Annotated[list[int], Field(ge=1, le=9)] = None): ...

    prop = _schema(handler)["n"]
    assert prop["items"]["minimum"] == 1
    assert prop["items"]["maximum"] == 9


def test_list_length_bounds_reach_the_openapi_document(client: TestClient):
    assert parameter(document(client), "/tags", "tag")["schema"]["maxItems"] == 3


def test_a_list_within_its_bounds_is_accepted(client: TestClient):
    response = client.get("/tags?tag=a&tag=b&tag=c")
    assert response.status_code == 200
    assert response.json() == {"tags": ["a", "b", "c"]}


def test_a_list_over_its_bound_is_rejected(client: TestClient):
    assert client.get("/tags?tag=a&tag=b&tag=c&tag=d").status_code == 422


def test_an_absent_list_still_takes_its_default(client: TestClient):
    assert client.get("/tags").json() == {"tags": None}


def test_a_list_marker_beside_a_field_wins_and_warns():
    def handler(tag: Annotated[list[str], Query(max_length=2), Field(max_length=9)] = None): ...

    with pytest.warns(UserWarning, match="tag.*Query"):
        props = _schema(handler)
    assert props["tag"]["maxItems"] == 2


def test_a_list_marker_only_parameter_is_unchanged(client: TestClient):
    assert client.get("/marked-tags?tag=a&tag=b").json() == {"tags": ["a", "b"]}
    assert client.get("/marked-tags?tag=a&tag=b&tag=c&tag=d").status_code == 422


# ── bindings a marker cannot describe ───────────────────────────────


class _Payload(BaseModel):
    a: int


def test_a_field_on_a_body_model_warns_rather_than_vanishing():
    def handler(payload: Annotated[_Payload, Field(description="x", min_length=2)]): ...

    with pytest.warns(UserWarning, match="payload.*request-body model"):
        build_plan(handler)


def test_a_field_on_an_upload_warns_rather_than_vanishing():
    def handler(upload: Annotated[UploadFile, Field(description="x", min_length=2)]): ...

    with pytest.warns(UserWarning, match="upload.*file upload"):
        build_plan(handler)


# ── the conflict warning names a remedy that exists ─────────────────


def test_a_field_beside_a_marker_says_where_to_declare_the_constraints():
    def handler(limit: Annotated[int, Field(ge=1)] = Query(le=10)):
        return limit

    with pytest.warns(UserWarning, match=r"declare them on the Query\(\) instead"):
        build_plan(handler)


def test_a_field_beside_a_depends_does_not_advise_declaring_them_there():
    """`Depends()` accepts no constraints, so the remedy clause is dropped
    rather than pointing the author at an argument that does not exist."""

    def dep() -> int:
        return 1

    def handler(limit: Annotated[int, Field(ge=1)] = Depends(dep)):
        return limit

    with pytest.warns(UserWarning) as caught:
        build_plan(handler)
    messages = [str(w.message) for w in caught if "Field()" in str(w.message)]
    assert len(messages) == 1
    assert "the marker wins" in messages[0]
    assert "declare them on" not in messages[0]


# ── every Field in the annotation counts, not just the first ─────────
#
# `Annotated` accumulates metadata, so a constraint can arrive split across
# several `Field()` entries - one from a shared type alias, another narrowing it
# at the parameter. Only the first was read, so the rest were dropped from both
# validation and the published schema: the document advertised a contract the
# resolver did not hold anyone to, and a client generated from it would send
# values the endpoint silently accepted.


def test_a_constraint_split_across_two_fields_is_enforced(client: TestClient):
    assert client.get("/split?level=5").status_code == 200
    assert client.get("/split?level=0").status_code == 422
    assert client.get("/split?level=99").status_code == 422


def test_a_constraint_split_across_two_fields_is_published(client: TestClient):
    schema = parameter(document(client.app), "/split", "level")["schema"]
    assert schema["minimum"] == 1
    assert schema["maximum"] == 10


def test_a_later_field_narrows_an_earlier_one(client: TestClient):
    """pydantic's own merge order: the last writer of a key wins."""
    assert client.get("/narrowed?level=5").status_code == 200
    assert client.get("/narrowed?level=1").status_code == 422


def test_a_later_field_narrows_what_the_schema_publishes(client: TestClient):
    schema = parameter(document(client.app), "/narrowed", "level")["schema"]
    assert schema["minimum"] == 5


def test_a_single_field_is_unchanged_by_the_merge(client: TestClient):
    """The control: one `Field` must behave exactly as it did before."""
    assert client.get("/level?level=5").status_code == 200
    assert client.get("/level?level=0").status_code == 422
    assert client.get("/level?level=99").status_code == 422


def test_the_agent_door_publishes_both_fields_too():
    """The schema the MCP door builds is derived from the same folded marker."""

    async def handler(level: Annotated[int, Field(ge=1), Field(le=10)] = 5):
        return {"level": level}

    schema = _schema(handler)["level"]
    assert schema["minimum"] == 1
    assert schema["maximum"] == 10


# ── a constraint written without the `Field()` wrapper ───────────────
#
# `Field(gt=0)` builds an `annotated_types.Gt(0)` and hands it to `Annotated`,
# so `Annotated[int, Gt(0)]` is the same declaration written one layer down -
# the spelling pydantic's own docs use. Only the wrapper was read, so the bare
# form was dropped from validation and from the published schema alike.


def test_a_bare_constraint_is_enforced(client: TestClient):
    assert client.get("/bare-gt?n=5").status_code == 200
    assert client.get("/bare-gt?n=0").status_code == 422
    assert client.get("/bare-gt?n=-1").status_code == 422


def test_a_bare_constraint_is_published(client: TestClient):
    schema = parameter(document(client.app), "/bare-gt", "n")["schema"]
    assert schema["exclusiveMinimum"] == 0


def test_bare_constraints_combine(client: TestClient):
    assert client.get("/bare-interval?n=5").status_code == 200
    assert client.get("/bare-interval?n=0").status_code == 422
    assert client.get("/bare-interval?n=99").status_code == 422


def test_a_bare_length_constraint_is_enforced(client: TestClient):
    assert client.get("/bare-len?code=abc").status_code == 200
    assert client.get("/bare-len?code=ab").status_code == 422


def test_an_explicit_field_beside_a_bare_constraint_wins(client: TestClient):
    """Both are read; the wrapper is the more specific declaration."""
    assert client.get("/bare-and-field?n=5").status_code == 200
    assert client.get("/bare-and-field?n=1").status_code == 422


def test_non_constraint_metadata_is_still_ignored(client: TestClient):
    """The negative: only the constraint vocabulary is folded in."""
    assert client.get("/plain?note=anything").status_code == 200


# ── `pattern` is searched, as the schema it publishes says ───────────


def test_a_pattern_is_searched_not_anchored(client: TestClient):
    """It is published verbatim as JSON Schema's `pattern`, a partial match."""
    assert client.get("/pattern?code=abc").status_code == 200
    assert client.get("/pattern?code=1abc").status_code == 200
    assert client.get("/pattern?code=abc1").status_code == 200


def test_a_pattern_that_matches_nowhere_is_still_refused(client: TestClient):
    assert client.get("/pattern?code=123").status_code == 422


def test_an_anchored_pattern_still_requires_a_full_match(client: TestClient):
    """Anchoring stays available, and is how a full match is asked for."""
    assert client.get("/anchored?code=abc").status_code == 200
    assert client.get("/anchored?code=1abc").status_code == 422
