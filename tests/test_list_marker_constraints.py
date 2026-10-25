"""Constraints declared on a list-typed parameter marker: what is published, what is enforced."""

from __future__ import annotations

import copy
import dataclasses
import datetime
import enum
import functools
import pathlib
import threading
import uuid
import warnings
from decimal import Decimal

import pytest

from tests._openapi import parameter
from veloce import Body, Depends, Query, Veloce
from veloce._handler_plan import build_plan
from veloce._params import deepcopyable, is_immutable
from veloce.contrib.mcp.plan_bridge import build_input_schema
from veloce.contrib.openapi import get_openapi_schema
from veloce.testclient import TestClient


def _schema(app: Veloce, path: str, name: str) -> dict:
    param = parameter(get_openapi_schema(app), path, name)
    assert param is not None
    schema: dict = param["schema"]
    return schema


# ── the published document ────────────────────────────────


def test_an_item_bound_is_published_as_min_max_items():
    app = Veloce()

    @app.get("/items")
    async def items(tags: list[str] = Query(default_factory=list, min_length=1, max_length=3)):
        return {"n": len(tags)}

    schema = _schema(app, "/items", "tags")
    assert schema["minItems"] == 1
    assert schema["maxItems"] == 3
    # `minLength` / `maxLength` constrain a string; an array carries neither.
    assert "minLength" not in schema
    assert "maxLength" not in schema


def test_scalar_bounds_are_published_on_the_items_subschema():
    app = Veloce()

    @app.get("/nums")
    async def nums(n: list[int] = Query(default_factory=list, ge=1, le=10, multiple_of=2)):
        return {"n": n}

    schema = _schema(app, "/nums", "n")
    assert schema["items"]["minimum"] == 1
    assert schema["items"]["maximum"] == 10
    assert schema["items"]["multipleOf"] == 2
    for keyword in ("minimum", "maximum", "multipleOf"):
        assert keyword not in schema


def test_exclusive_bounds_and_pattern_are_published_on_the_items_subschema():
    app = Veloce()

    @app.get("/mixed")
    async def mixed(
        n: list[int] = Query(default_factory=list, gt=0, lt=5),
        code: list[str] = Query(default_factory=list, pattern=r"^[A-Z]{3}$"),
    ):
        return {}

    numbers = _schema(app, "/mixed", "n")
    assert numbers["items"]["exclusiveMinimum"] == 0
    assert numbers["items"]["exclusiveMaximum"] == 5
    assert "exclusiveMinimum" not in numbers

    codes = _schema(app, "/mixed", "code")
    assert codes["items"]["pattern"] == r"^[A-Z]{3}$"
    assert "pattern" not in codes


def test_an_unconstrained_list_publishes_no_bounds():
    app = Veloce()

    @app.get("/plain")
    async def plain(tags: list[str] = Query(default_factory=list)):
        return {"n": len(tags)}

    assert _schema(app, "/plain", "tags") == {"type": "array", "items": {"type": "string"}}


def test_a_bare_list_annotation_does_not_leak_its_items_schema():
    app = Veloce()

    @app.get("/one")
    async def one(tags: list = Query(default_factory=list, ge=1)):
        return {}

    @app.get("/two")
    async def two(tags: list = Query(default_factory=list)):
        return {}

    # The untyped `list` schema comes from a shared table; writing a bound
    # through its `items` dict would publish it on every other `list` too.
    assert _schema(app, "/one", "tags")["items"] == {"minimum": 1}
    assert _schema(app, "/two", "tags")["items"] == {}


# ── enforcement ───────────────────────────────────────────


def test_a_list_within_its_item_bound_is_accepted():
    app = Veloce()

    @app.get("/items")
    async def items(tags: list[str] = Query(default_factory=list, max_length=3)):
        return {"n": len(tags)}

    with TestClient(app) as client:
        response = client.get("/items?tags=a&tags=b&tags=c")
    assert response.status_code == 200
    assert response.json() == {"n": 3}


def test_a_list_over_its_item_bound_is_rejected():
    app = Veloce()

    @app.get("/items")
    async def items(tags: list[str] = Query(default_factory=list, max_length=3)):
        return {"n": len(tags)}

    with TestClient(app) as client:
        response = client.get("/items?tags=a&tags=b&tags=c&tags=d&tags=e")
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["loc"] == ["query", "tags"]
    assert detail["msg"] == "tags must have at most 3 items"


def test_a_list_under_its_item_bound_is_rejected():
    app = Veloce()

    @app.get("/items")
    async def items(tags: list[str] = Query(default_factory=list, min_length=2)):
        return {"n": len(tags)}

    with TestClient(app) as client:
        response = client.get("/items?tags=a")
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "tags must have at least 2 items"


def test_an_element_outside_a_scalar_bound_is_rejected():
    app = Veloce()

    @app.get("/nums")
    async def nums(n: list[int] = Query(default_factory=list, ge=1, le=10)):
        return {"total": sum(n)}

    with TestClient(app) as client:
        assert client.get("/nums?n=1&n=10").json() == {"total": 11}
        response = client.get("/nums?n=1&n=99")
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["loc"] == ["query", "n"]
    assert detail["msg"] == "n must be <= 10"


def test_an_element_failing_the_pattern_is_rejected():
    app = Veloce()

    @app.get("/codes")
    async def codes(code: list[str] = Query(default_factory=list, pattern=r"^[A-Z]{3}$")):
        return {"codes": code}

    with TestClient(app) as client:
        assert client.get("/codes?code=ABC&code=XYZ").status_code == 200
        response = client.get("/codes?code=ABC&code=nope")
    assert response.status_code == 422
    assert "does not match pattern" in response.json()["detail"][0]["msg"]


def test_an_item_bound_does_not_constrain_element_length():
    app = Veloce()

    @app.get("/items")
    async def items(tags: list[str] = Query(default_factory=list, max_length=2)):
        return {"tags": tags}

    # `max_length` bounds the item count, so a long element is still fine.
    with TestClient(app) as client:
        response = client.get("/items?tags=aaaaaaaaaa")
    assert response.status_code == 200
    assert response.json() == {"tags": ["aaaaaaaaaa"]}


def test_an_unconstrained_list_accepts_any_number_of_values():
    app = Veloce()

    @app.get("/plain")
    async def plain(tags: list[str] = Query(default_factory=list)):
        return {"n": len(tags)}

    with TestClient(app) as client:
        response = client.get("/plain?" + "&".join(f"tags={i}" for i in range(20)))
    assert response.json() == {"n": 20}


def test_an_absent_list_falls_back_to_its_default():
    app = Veloce()

    @app.get("/items")
    async def items(tags: list[str] = Query(default_factory=list, min_length=2)):
        return {"n": len(tags)}

    # A default is returned unvalidated, exactly as on the scalar path.
    with TestClient(app) as client:
        response = client.get("/items")
    assert response.status_code == 200
    assert response.json() == {"n": 0}


# ── both resolvers ────────────────────────────────────────


async def _noop() -> None:
    return None


def test_the_interpreter_enforces_the_same_item_bound():
    app = Veloce()

    # A route-level dependency keeps the plan off both compiled resolvers, so
    # this exercises `DependencyResolver._resolve_marker` itself.
    @app.get("/items", dependencies=[Depends(_noop)])
    async def items(tags: list[str] = Query(default_factory=list, max_length=3)):
        return {"n": len(tags)}

    with TestClient(app) as client:
        assert client.get("/items?tags=a&tags=b&tags=c").status_code == 200
        response = client.get("/items?tags=a&tags=b&tags=c&tags=d&tags=e")
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "tags must have at most 3 items"


def test_the_graph_resolver_enforces_the_same_item_bound():
    app = Veloce()

    # A handler-level dependency moves the plan onto the compiled *graph*
    # resolver, the third path that binds a list-typed marker.
    @app.get("/items")
    async def items(
        tags: list[str] = Query(default_factory=list, max_length=3),
        _dep: None = Depends(_noop),
    ):
        return {"n": len(tags)}

    with TestClient(app) as client:
        assert client.get("/items?tags=a&tags=b").status_code == 200
        response = client.get("/items?tags=a&tags=b&tags=c&tags=d")
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "tags must have at most 3 items"


def test_a_json_array_body_is_held_to_its_published_item_bound():
    app = Veloce()

    @app.post("/bulk")
    async def bulk(payload: list[str] = Body(max_length=2)):
        return {"n": len(payload)}

    body_schema = get_openapi_schema(app)["paths"]["/bulk"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert body_schema["maxItems"] == 2
    assert "maxLength" not in body_schema

    with TestClient(app) as client:
        assert client.post("/bulk", json=["a", "b"]).status_code == 200
        response = client.post("/bulk", json=["a", "b", "c"])
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["loc"] == ["body", "payload"]
    assert detail["msg"] == "payload must have at most 2 items"


def test_the_interpreter_enforces_the_same_element_bound():
    app = Veloce()

    @app.get("/nums", dependencies=[Depends(_noop)])
    async def nums(n: list[int] = Query(default_factory=list, ge=1)):
        return {"total": sum(n)}

    with TestClient(app) as client:
        assert client.get("/nums?n=2").status_code == 200
        response = client.get("/nums?n=0")
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "n must be >= 1"


# ── the scalar path is unchanged ───────────────────────────


def test_a_scalar_parameter_still_publishes_and_enforces_max_length():
    app = Veloce()

    @app.get("/q")
    async def q(term: str = Query(default="", max_length=3)):
        return {"term": term}

    schema = _schema(app, "/q", "term")
    assert schema["maxLength"] == 3
    assert "maxItems" not in schema

    with TestClient(app) as client:
        assert client.get("/q?term=abc").status_code == 200
        response = client.get("/q?term=abcd")
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "term must have at most 3 characters"


# ── a JSON array body is held to its declared element type ───────────
#
# The members of a JSON body arrive already typed by the decode, so the declared
# `list[T]` is a contract to check rather than a string to parse. It was parsed:
# `1.5` became `1` and `true` became `1` for a `list[int]`, and a `list[str]`
# took whatever it was given. Worse, the coercion ran only when a bound was also
# declared, so the same body meant two different things either side of a
# `max_length=` - adding a limit silently changed the values the handler saw.


def test_a_fractional_number_is_refused_for_a_declared_int_member():
    app = Veloce()

    @app.post("/ints")
    async def ints(payload: list[int] = Body(max_length=3)):
        return {"payload": payload}

    with TestClient(app) as client:
        response = client.post("/ints", json=[1.5, 2])
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", 0]


def test_a_boolean_is_read_as_an_int_member_as_the_declared_type_allows():
    """Not a refusal: `bool` is an `int` subtype and the adapter admits it.

    Pinned because the distinction is the whole point of validating through the
    declared type - `1.5` is refused and `True` is not, and that is a property
    of the type rather than of this code.
    """
    app = Veloce()

    @app.post("/ints")
    async def ints(payload: list[int] = Body(max_length=3)):
        return {"payload": payload, "types": [type(v).__name__ for v in payload]}

    with TestClient(app) as client:
        response = client.post("/ints", json=[True])
    assert response.status_code == 200
    assert response.json() == {"payload": [1], "types": ["int"]}


def test_a_number_is_refused_for_a_declared_str_member():
    app = Veloce()

    @app.post("/strs")
    async def strs(payload: list[str] = Body(max_length=3)):
        return {"payload": payload}

    with TestClient(app) as client:
        response = client.post("/strs", json=[1, 2])
    assert response.status_code == 422


def test_a_bound_does_not_decide_whether_the_element_type_is_checked():
    """The defect: the same body was accepted or rewritten depending on a bound."""
    bounded = Veloce()
    unbounded = Veloce()

    @bounded.post("/ints")
    async def with_bound(payload: list[int] = Body(max_length=3)):
        return {"payload": payload}

    @unbounded.post("/ints")
    async def without_bound(payload: list[int] = Body()):
        return {"payload": payload}

    with TestClient(bounded) as client:
        first = client.post("/ints", json=[1.5])
    with TestClient(unbounded) as client:
        second = client.post("/ints", json=[1.5])
    assert first.status_code == second.status_code == 422


def test_a_well_formed_numeric_string_is_still_accepted():
    """The positive: lax coercion of a string that really is an integer stays."""
    app = Veloce()

    @app.post("/ints")
    async def ints(payload: list[int] = Body(max_length=3)):
        return {"payload": payload, "types": [type(v).__name__ for v in payload]}

    with TestClient(app) as client:
        response = client.post("/ints", json=["1", "2"])
    assert response.status_code == 200
    assert response.json() == {"payload": [1, 2], "types": ["int", "int"]}


def test_a_valid_array_body_is_unchanged():
    """The control: the members a declared `list[int]` allows arrive untouched."""
    app = Veloce()

    @app.post("/ints")
    async def ints(payload: list[int] = Body(max_length=3)):
        return {"payload": payload, "types": [type(v).__name__ for v in payload]}

    with TestClient(app) as client:
        response = client.post("/ints", json=[1, 2])
    assert response.status_code == 200
    assert response.json() == {"payload": [1, 2], "types": ["int", "int"]}


def test_an_unbounded_array_body_still_reaches_the_handler():
    app = Veloce()

    @app.post("/strs")
    async def strs(payload: list[str] = Body()):
        return {"payload": payload}

    with TestClient(app) as client:
        response = client.post("/strs", json=["a", "b"])
    assert response.status_code == 200
    assert response.json() == {"payload": ["a", "b"]}


# ── a sequence body is validated as the type it declares ─────────────
#
# The members were validated against `list[<first type argument>]`, which is
# only the declared type for a homogeneous sequence. A `tuple[int, str]` was
# rebuilt as `list[int]`, so its own second member was refused.


def test_a_fixed_shape_tuple_body_validates_each_position():
    app = Veloce()

    @app.post("/pair")
    async def pair(payload: tuple[int, str] = Body()) -> dict:
        return {"payload": list(payload), "types": [type(v).__name__ for v in payload]}

    with TestClient(app) as client:
        response = client.post("/pair", json=[1, "a"])
    assert response.status_code == 200
    assert response.json() == {"payload": [1, "a"], "types": ["int", "str"]}


def test_a_fixed_shape_tuple_body_still_refuses_a_wrong_position():
    app = Veloce()

    @app.post("/pair")
    async def pair(payload: tuple[int, str] = Body()) -> dict:
        return {"payload": list(payload)}

    with TestClient(app) as client:
        assert client.post("/pair", json=["a", "b"]).status_code == 422


def test_a_set_body_comes_back_as_the_set_it_declares():
    app = Veloce()

    @app.post("/tags")
    async def tags(payload: set[str] = Body()) -> dict:
        return {"type": type(payload).__name__, "n": len(payload)}

    with TestClient(app) as client:
        response = client.post("/tags", json=["a", "a", "b"])
    assert response.status_code == 200
    assert response.json() == {"type": "set", "n": 2}


def test_a_homogeneous_list_body_is_unchanged():
    """The control: the shape this path was written for still behaves."""
    app = Veloce()

    @app.post("/ints")
    async def ints(payload: list[int] = Body(max_length=3)) -> dict:
        return {"payload": payload}

    with TestClient(app) as client:
        assert client.post("/ints", json=[1, 2]).json() == {"payload": [1, 2]}
        assert client.post("/ints", json=[1, 2, 3, 4]).status_code == 422


# ── `tuple[()]` declares no positions ────────────────────────────────


def test_an_empty_tuple_parameter_accepts_no_values():
    """`tuple[()]` and a bare `tuple` both decompose to no arguments, and
    reading them the same way let the empty tuple accept anything."""
    app = Veloce()

    @app.get("/none")
    async def none_at_all(p: tuple[()] = Query(default=())) -> dict:
        return {"p": list(p)}

    with TestClient(app) as client:
        assert client.get("/none").json() == {"p": []}
        assert client.get("/none?p=1").status_code == 422
        assert client.get("/none?p=1&p=2").status_code == 422


def test_a_variadic_tuple_parameter_still_takes_many_values():
    """The control: `tuple[int, ...]` is one element type for every member, so
    it must not be read as a fixed shape of one position."""
    app = Veloce()

    @app.get("/many")
    async def many(p: tuple[int, ...] = Query(default_factory=tuple)) -> dict:
        return {"type": type(p).__name__, "p": list(p)}

    with TestClient(app) as client:
        assert client.get("/many?p=1&p=2").json() == {"type": "tuple", "p": [1, 2]}


# ── a default arrives as the container the signature declares ────────


def test_a_list_valued_default_for_a_set_arrives_as_a_set():
    """The signature says set and the default said list, so the handler got a
    list from a parameter whose annotation forbids one."""
    app = Veloce()

    @app.get("/tags")
    async def tags(t: set[str] = Query(default=["a", "b"])) -> dict:
        return {"type": type(t).__name__, "v": sorted(t)}

    with TestClient(app) as client:
        assert client.get("/tags").json() == {"type": "set", "v": ["a", "b"]}


def test_a_list_valued_body_default_arrives_as_a_set():
    app = Veloce()

    @app.post("/tags")
    async def tags(t: set[str] = Body(default=["a", "a"])) -> dict:
        return {"type": type(t).__name__, "v": sorted(t)}

    with TestClient(app) as client:
        assert client.post("/tags", content=b"").json() == {"type": "set", "v": ["a"]}


def test_a_factory_default_for_a_frozenset_arrives_frozen():
    app = Veloce()

    @app.get("/tags")
    async def tags(t: frozenset[int] = Query(default_factory=lambda: [1, 1, 2])) -> dict:
        return {"type": type(t).__name__, "n": len(t)}

    with TestClient(app) as client:
        assert client.get("/tags").json() == {"type": "frozenset", "n": 2}


def test_a_matching_default_is_left_alone():
    """The control: a default already of the declared type is untouched."""
    app = Veloce()

    @app.get("/tags")
    async def tags(t: set[str] = Query(default_factory=set)) -> dict:
        return {"type": type(t).__name__, "v": sorted(t)}

    with TestClient(app) as client:
        assert client.get("/tags").json() == {"type": "set", "v": []}


# ── the document describes the shape the resolver enforces ───────────


def test_a_fixed_tuple_publishes_a_type_per_position_and_a_length():
    """Published as a homogeneous array of the first type, the schema described
    a shape the resolver refuses - so a client generated from it sent one."""
    app = Veloce()

    @app.post("/pair")
    async def pair(payload: tuple[int, str] = Body()) -> dict:
        return {}

    schema = get_openapi_schema(app)["paths"]["/pair"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert schema["prefixItems"] == [{"type": "integer"}, {"type": "string"}]
    assert schema["minItems"] == 2
    assert schema["maxItems"] == 2


def test_a_set_publishes_unique_items():
    app = Veloce()

    @app.get("/tags")
    async def tags(t: set[str] = Query(default_factory=set)) -> dict:
        return {}

    assert _schema(app, "/tags", "t")["uniqueItems"] is True


def test_a_frozenset_publishes_an_array_schema():
    app = Veloce()

    @app.get("/tags")
    async def tags(t: frozenset[int] = Query(default_factory=frozenset)) -> dict:
        return {}

    schema = _schema(app, "/tags", "t")
    assert schema["type"] == "array"
    assert schema["items"] == {"type": "integer"}
    assert schema["uniqueItems"] is True


def test_a_list_publishes_no_unique_items():
    """The negative: only a set deduplicates, so only a set says so."""
    app = Veloce()

    @app.get("/tags")
    async def tags(t: list[str] = Query(default_factory=list)) -> dict:
        return {}

    assert "uniqueItems" not in _schema(app, "/tags", "t")


# ── a normalised default is fresh, and belongs to its own route ──────
#
# Rebuilding the default into the declared container introduced two hazards at
# once: an immutable default converted into a `set` and kept was one object
# every request shared - and the shared-mutable-default warning runs before the
# conversion, so nothing said so - while writing it back into the marker let a
# marker shared by two routes be decided by whichever registered last.


@pytest.mark.parametrize(
    "default",
    [("a",), frozenset({"a"}), ["a"], {"a"}],
    ids=["tuple", "frozenset", "list", "set"],
)
def test_a_converted_set_default_is_fresh_for_every_request(default):
    app = Veloce()
    seen: list[int] = []

    @app.get("/tags")
    async def tags(t: set[str] = Query(default=default)) -> dict:
        t.add(f"req{len(seen)}")
        seen.append(len(t))
        return {"n": len(t)}

    with TestClient(app) as client:
        sizes = [client.get("/tags").json()["n"] for _ in range(4)]
    # Two every time: the default's one member plus this request's own.
    assert sizes == [2, 2, 2, 2]


def test_one_marker_shared_by_two_routes_serves_each_its_own_container():
    shared = Query(default=["a"])
    app = Veloce()

    @app.get("/as-tuple")
    async def as_tuple(m: tuple[str, ...] = shared) -> dict:
        return {"type": type(m).__name__}

    @app.get("/as-set")
    async def as_set(m: set[str] = shared) -> dict:
        return {"type": type(m).__name__}

    with TestClient(app) as client:
        assert client.get("/as-tuple").json() == {"type": "tuple"}
        assert client.get("/as-set").json() == {"type": "set"}


def test_the_shared_marker_itself_is_left_alone():
    """The marker the author holds must not be rewritten under them."""
    shared = Query(default=["a"])
    before = shared.default
    app = Veloce()

    @app.get("/x")
    async def x(m: set[str] = shared) -> dict:
        return {"type": type(m).__name__}

    with TestClient(app) as client:
        client.get("/x")
    assert shared.default == before
    assert type(shared.default) is list


def test_an_immutable_declared_container_keeps_an_immutable_default():
    """A `tuple` default needs no factory; sharing one is safe because it is
    immutable, and that is the reason the set case needs a factory."""
    app = Veloce()

    @app.get("/pair")
    async def pair(m: tuple[str, ...] = Query(default=["a", "b"])) -> dict:
        return {"type": type(m).__name__, "v": list(m)}

    with TestClient(app) as client:
        assert client.get("/pair").json() == {"type": "tuple", "v": ["a", "b"]}


# ── a parameter with no marker is a different shape ──────────────────


def test_a_bare_sequence_annotation_is_not_a_repeated_parameter():
    """Documented so the container fixes are not read as covering it.

    `m: set[int]` with no marker is not a repeated-value parameter - it is a
    single value the resolver tries to read as a set - and it answers 422. The
    container work above applies to a marker-declared parameter.
    """
    app = Veloce()

    @app.get("/bare")
    async def bare(m: set[int]) -> dict:
        return {"n": len(m)}

    with TestClient(app) as client:
        assert client.get("/bare?m=1&m=2").status_code == 422


# ── element keywords go where the array's shape puts them ────────────
#
# A homogeneous array has one `items` subschema; a fixed-shape tuple has one
# entry per position and no `items` at all. Writing `items` unconditionally
# added an empty subschema to every tuple schema, and would have hung an
# element constraint off a keyword the tuple does not use.


def test_a_fixed_tuple_schema_carries_no_items():
    app = Veloce()

    @app.post("/pair")
    async def pair(payload: tuple[int, str] = Body()) -> dict:
        return {}

    schema = get_openapi_schema(app)["paths"]["/pair"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert "items" not in schema
    assert schema["prefixItems"] == [{"type": "integer"}, {"type": "string"}]


def test_an_element_constraint_lands_on_every_tuple_position():
    app = Veloce()

    @app.get("/pair")
    async def pair(p: tuple[int, int] = Query(default=(1, 2), ge=1)) -> dict:
        return {}

    schema = _schema(app, "/pair", "p")
    assert schema["prefixItems"] == [
        {"type": "integer", "minimum": 1},
        {"type": "integer", "minimum": 1},
    ]
    assert "items" not in schema


def test_an_element_constraint_still_lands_on_items_for_a_list():
    """The control: a homogeneous array keeps the single `items` subschema."""
    app = Veloce()

    @app.get("/nums")
    async def nums(n: list[int] = Query(default_factory=list, ge=1, le=9)) -> dict:
        return {}

    schema = _schema(app, "/nums", "n")
    assert schema["items"] == {"type": "integer", "minimum": 1, "maximum": 9}
    assert "prefixItems" not in schema


def test_a_list_with_no_element_constraint_keeps_its_item_type():
    app = Veloce()

    @app.get("/tags")
    async def tags(t: list[str] = Query(default_factory=list, max_length=3)) -> dict:
        return {}

    schema = _schema(app, "/tags", "t")
    assert schema["items"] == {"type": "string"}
    assert schema["maxItems"] == 3


def test_the_two_doors_publish_the_same_array_schema():
    """The agent door derives its schema from the same walk."""
    app = Veloce()

    @app.post("/pair")
    async def pair(payload: tuple[int, str] = Body()) -> dict:
        return {}

    http = get_openapi_schema(app)["paths"]["/pair"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    agent = build_input_schema(build_plan(pair), {})["properties"]["payload"]
    assert http == agent


# ── what a converted default may share, decided by what is in it ─────


def test_a_set_default_that_cannot_be_built_is_left_as_written():
    """A declared `set` whose default holds an unhashable member cannot be
    converted at all, and raising from the factory turned that authoring
    mistake into a 500 on every request that omitted the value."""
    app = Veloce()

    @app.get("/u")
    async def u(m: set[str] = Query(default=(["a"],))) -> dict:
        return {"type": type(m).__name__, "n": len(m)}

    with TestClient(app) as client:
        response = client.get("/u")
    assert response.status_code == 200
    assert response.json() == {"type": "tuple", "n": 1}


def test_a_tuple_default_with_a_mutable_member_is_copied_per_request():
    """A `tuple` is immutable only at the top: `(["a"],)` would hand every
    request the same inner list."""
    app = Veloce()

    @app.get("/m")
    async def m(v: tuple[list[str], ...] = Query(default=(["a"],))) -> dict:
        v[0].append("leaked")
        return {"inner": len(v[0])}

    with TestClient(app) as client:
        assert [client.get("/m").json()["inner"] for _ in range(3)] == [2, 2, 2]


def test_a_normalised_default_does_not_warn_about_sharing():
    """The warning named a sharing the conversion had already removed."""
    app = Veloce()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        @app.get("/t")
        async def t(m: set[str] = Query(default=["a"])) -> dict:
            return {"n": len(m)}

    assert [w for w in caught if "shared across requests" in str(w.message)] == []


def test_a_plain_list_parameter_still_warns_about_a_mutable_default():
    """The negative: the warning still fires where the sharing is real."""
    app = Veloce()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        @app.get("/l")
        async def l_(m: list[str] = Query(default=["a"])) -> dict:
            return {"n": len(m)}

    assert [w for w in caught if "shared across requests" in str(w.message)]


class _Colour(enum.Enum):
    red = 1
    green = 2


@dataclasses.dataclass(frozen=True)
class _Point:
    x: int
    y: int


@dataclasses.dataclass(frozen=True)
class _Bag:
    contents: list[str]


@dataclasses.dataclass(frozen=True, slots=True)
class _SlottedRows:
    rows: list[str]


@dataclasses.dataclass(frozen=True)
class _ChildOfSlotted(_SlottedRows):
    label: str = "x"


@dataclasses.dataclass(frozen=True)
class _ExtraSlot:
    __slots__ = ("x", "stash")
    x: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "stash", [])


@dataclasses.dataclass(frozen=True)
class _SlotBase:
    __slots__ = ("rows",)
    rows: list[str]


class _ShadowedSlot(_SlotBase):
    __slots__ = ()
    rows = ()  # type: ignore[assignment]


_SLOT_DESCRIPTOR = vars(_SlotBase)["rows"]


@dataclasses.dataclass(frozen=True)
class _Private:
    __slots__ = ("__hidden",)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_Private__hidden", [])

    @property
    def rows(self) -> list[str]:
        return self._Private__hidden  # type: ignore[attr-defined,no-any-return]


@dataclasses.dataclass(frozen=True)
class _Lazy:
    __slots__ = ("x", "cached")
    x: int

    def __getattr__(self, name: str) -> object:
        # Consulted only for a slot that was never assigned.
        raise RuntimeError(f"{name} is not ready")


@dataclasses.dataclass(frozen=True)
class _UnsetSlot:
    __slots__ = ("x", "spare")
    x: int


@dataclasses.dataclass(frozen=True)
class _Node:
    child: object = None


@dataclasses.dataclass(frozen=True)
class _Sneaky:
    x: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", [])


@dataclasses.dataclass(frozen=True)
class _Cached:
    x: int

    @functools.cached_property
    def tally(self) -> list[str]:
        return []


class _MutableTz(datetime.tzinfo):
    """A tzinfo with state of its own, which nothing about `datetime` forbids."""

    def __init__(self) -> None:
        self.log: list[str] = []

    def utcoffset(self, dt: datetime.datetime | None) -> datetime.timedelta:
        return datetime.timedelta(0)

    def tzname(self, dt: datetime.datetime | None) -> str:
        return "MUT"

    def dst(self, dt: datetime.datetime | None) -> datetime.timedelta | None:
        return None


class _Mutable:
    """Hashable by identity - the default `__hash__` - and still mutable."""

    def __init__(self) -> None:
        self.seen: list[str] = []


class _HoldsLock:
    """Cannot be deep-copied: a lock is not picklable."""

    def __init__(self) -> None:
        self.lock = threading.Lock()


class _Tag(str):
    """A `str` subclass, so it carries a `__dict__` its base does not."""

    def __new__(cls, value: str) -> _Tag:
        created = super().__new__(cls, value)
        created.seen = []
        return created


def test_a_hashable_member_with_mutable_state_is_copied_per_request():
    """Hashability was the wrong question. A plain object hashes fine and
    carries mutable attributes, so sharing it let one request's writes arrive
    in the next."""
    app = Veloce()

    @app.get("/h")
    async def h(v: tuple[_Mutable, ...] = Query(default=(_Mutable(),))) -> dict:
        v[0].seen.append("x")
        return {"n": len(v[0].seen)}

    with TestClient(app) as client:
        assert [client.get("/h").json()["n"] for _ in range(3)] == [1, 1, 1]


def test_a_nested_tuple_holding_a_mutable_member_is_copied_per_request():
    """`isinstance((["a"],), Hashable)` is `True` though hashing it raises, so
    a member-level hashability check called this one shareable."""
    app = Veloce()

    @app.get("/n")
    async def n(v: tuple[tuple[list[str], ...], ...] = Query(default=((["a"],),))) -> dict:
        v[0][0].append("x")
        return {"inner": len(v[0][0])}

    with TestClient(app) as client:
        assert [client.get("/n").json()["inner"] for _ in range(3)] == [2, 2, 2]


def test_a_set_default_of_mutable_members_is_copied_per_request():
    """A declared `set` is rebuilt per request from a snapshot of the members,
    which is no protection when a member has state of its own."""
    app = Veloce()

    @app.get("/s")
    async def s(v: set[_Mutable] = Query(default=(_Mutable(),))) -> dict:
        next(iter(v)).seen.append("x")
        return {"n": len(next(iter(v)).seen)}

    with TestClient(app) as client:
        assert [client.get("/s").json()["n"] for _ in range(3)] == [1, 1, 1]


def test_a_marker_with_a_mutable_member_does_not_leak_between_routes():
    """One marker can be shared by two handlers, so the copy has to be per
    request rather than per route."""
    app = Veloce()
    shared = Query(default=(_Mutable(),))

    @app.get("/w")
    async def w(v: tuple[_Mutable, ...] = shared) -> dict:
        v[0].seen.append("x")
        return {"n": len(v[0].seen)}

    @app.get("/r")
    async def r(v: tuple[_Mutable, ...] = shared) -> dict:
        return {"n": len(v[0].seen)}

    with TestClient(app) as client:
        client.get("/w")
        assert client.get("/r").json() == {"n": 0}


def test_an_immutable_tuple_default_is_the_same_object_every_request():
    """The negative: nothing reachable through it can change, so it is served
    as one value rather than copied - the copying is only for the rest."""
    app = Veloce()
    seen: list[int] = []

    @app.get("/i")
    async def i(v: tuple[str, ...] = Query(default=("a", ("b",)))) -> dict:
        seen.append(id(v))
        return {"n": len(v)}

    with TestClient(app) as client:
        for _ in range(3):
            assert client.get("/i").status_code == 200
    assert len(set(seen)) == 1


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (lambda: "ab", "str"),
        (lambda: {"a": 1}, "dict"),
        (lambda: 5, "int"),
    ],
    ids=["string", "mapping", "scalar"],
)
def test_a_factory_default_that_is_not_a_sequence_is_left_as_written(factory, expected):
    """`set` accepts anything iterable and answers with something else: the
    string became `{"a", "b"}` and the mapping became the set of its keys."""
    app = Veloce()

    @app.get("/f")
    async def f(m: set[str] = Query(default_factory=factory)) -> dict:
        return {"type": type(m).__name__}

    with TestClient(app) as client:
        assert client.get("/f").json() == {"type": expected}


def test_a_member_that_cannot_be_deep_copied_is_shared_rather_than_raising():
    """A member holding a lock cannot be copied, and raising from the factory
    answered every request that omitted the value with a 500."""
    app = Veloce()

    @app.get("/k")
    async def k(xs: tuple[_HoldsLock, ...] = Query(default=(_HoldsLock(),))) -> dict:
        return {"n": len(xs)}

    with TestClient(app) as client:
        response = client.get("/k")
    assert response.status_code == 200
    assert response.json() == {"n": 1}


def test_a_str_subclass_member_is_copied_per_request():
    """`isinstance` is not the immutability test either: a `str` subclass
    carries a `__dict__` its base does not."""
    app = Veloce()

    @app.get("/sub")
    async def sub(tags: tuple[_Tag, ...] = Query(default=(_Tag("x"),))) -> dict:
        tags[0].seen.append("a")
        return {"seen": list(tags[0].seen)}

    with TestClient(app) as client:
        assert [client.get("/sub").json()["seen"] for _ in range(3)] == [["a"], ["a"], ["a"]]


def test_a_mutable_member_in_a_tuple_default_is_named_by_the_warning():
    """`default_factory=tuple` is not the fix when the container is immutable
    and its member is not, so the warning names the member instead."""
    app = Veloce()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        @app.get("/w")
        async def w(v: list[list[str]] = Query(default=(["a"],))) -> dict:
            return {"n": len(v)}

    messages = [
        str(entry.message) for entry in caught if "shared across requests" in str(entry.message)
    ]
    assert len(messages) == 1
    assert "tuple default holding a mutable member" in messages[0]
    assert "default_factory=tuple" not in messages[0]


def test_a_tuple_default_arrives_as_the_declared_list():
    """A `list` annotation names the container too, and the handler received
    the tuple the default was written as."""
    app = Veloce()

    @app.get("/l")
    async def l_(v: list[str] = Query(default=("a", "b"))) -> dict:
        v.append("own")
        return {"type": type(v).__name__, "v": v}

    with TestClient(app) as client:
        for _ in range(3):
            assert client.get("/l").json() == {"type": "list", "v": ["a", "b", "own"]}


def test_a_non_sequence_static_default_is_left_as_written():
    """The static twin of the `default_factory` case: there is no sequence to
    convert, so the value is handed over as it was declared."""
    app = Veloce()

    @app.get("/s")
    async def s(m: set[str] = Query(default="ab")) -> dict:
        return {"type": type(m).__name__}

    with TestClient(app) as client:
        assert client.get("/s").json() == {"type": "str"}


def test_a_factory_default_that_cannot_be_built_is_left_as_written():
    """The per-request twin of the registration-time bail-out: a declared
    `set` whose factory returns an unhashable member would raise on every
    request that omits the value."""
    app = Veloce()

    @app.get("/u")
    async def u(m: set[str] = Query(default_factory=lambda: [["x"]])) -> dict:
        return {"type": type(m).__name__, "n": len(m)}

    with TestClient(app) as client:
        response = client.get("/u")
    assert response.status_code == 200
    assert response.json() == {"type": "list", "n": 1}


@pytest.mark.parametrize(
    "member",
    [
        _Colour.red,
        datetime.date(2020, 1, 1),
        datetime.datetime(2020, 1, 1, 12),
        datetime.timedelta(days=1),
        Decimal("1.5"),
        uuid.UUID(int=1),
        pathlib.PurePosixPath("/tmp"),
    ],
    ids=["enum", "date", "datetime", "timedelta", "decimal", "uuid", "path"],
)
def test_a_stdlib_immutable_member_is_shared_not_copied(member):
    """Nothing reachable through these can change, so copying one per request
    is pure cost - and a deep copy of an `Enum` member hands back the member,
    so it does not even isolate anything."""
    app = Veloce()
    marker = Query(default=(member,))

    @app.get("/v")
    async def v(items: tuple[object, ...] = marker) -> dict:
        return {"n": len(items)}

    with TestClient(app) as client:
        assert client.get("/v").status_code == 200
    assert marker.default_factory is None


def test_a_stdlib_immutable_member_does_not_warn():
    """The warning named a mutable member where there was none, on ordinary
    code. The mutable parameter registered in the same capture is the control,
    so a capture that sees nothing at all cannot pass this."""
    app = Veloce()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        @app.get("/w")
        async def w(colours: tuple[_Colour, ...] = (_Colour.red, _Colour.green)) -> dict:
            return {"n": len(colours)}

        @app.get("/m")
        async def m(rows: tuple[list[str], ...] = (["a"],)) -> dict:
            return {"n": len(rows)}

    messages = [str(e.message) for e in caught if "shared across requests" in str(e.message)]
    assert len(messages) == 1
    assert "'rows'" in messages[0]


def test_a_frozen_dataclass_member_is_shared_when_its_fields_are_immutable():
    app = Veloce()
    marker = Query(default=(_Point(1, 2),))

    @app.get("/p")
    async def p(items: tuple[_Point, ...] = marker) -> dict:
        return {"n": len(items)}

    with TestClient(app) as client:
        assert client.get("/p").status_code == 200
    assert marker.default_factory is None


def test_a_frozen_dataclass_holding_a_list_is_copied_per_request():
    """The negative: frozen rebinds nothing, but a field can still hold a list
    for a handler to append to."""
    app = Veloce()

    @app.get("/b")
    async def b(items: tuple[_Bag, ...] = Query(default=(_Bag(["seed"]),))) -> dict:
        items[0].contents.append("x")
        return {"n": len(items[0].contents)}

    with TestClient(app) as client:
        assert [client.get("/b").json()["n"] for _ in range(3)] == [2, 2, 2]


def test_a_default_nested_beyond_the_depth_bound_registers_and_serves():
    """Recursing without a bound raised `RecursionError` at registration, so
    the route could not be declared at all."""
    app = Veloce()
    deep: object = ()
    for _ in range(3000):
        deep = (deep,)

    @app.get("/deep")
    async def d(v: tuple[object, ...] = Query(default=(deep,))) -> dict:
        return {"n": len(v)}

    with TestClient(app) as client:
        assert client.get("/deep").json() == {"n": 1}


def test_a_self_referencing_default_registers_and_serves():
    """A frozen dataclass can be made to hold itself, and following it without
    a bound never terminates."""
    app = Veloce()
    node = _Node()
    object.__setattr__(node, "child", node)

    @app.get("/cycle")
    async def c(v: tuple[_Node, ...] = Query(default=(node,))) -> dict:
        return {"n": len(v)}

    with TestClient(app) as client:
        assert client.get("/cycle").json() == {"n": 1}


def test_state_a_frozen_dataclass_adds_outside_its_fields_is_copied():
    """`__post_init__` writes through `object.__setattr__`, so reading the
    declared field list called this instance immutable."""
    app = Veloce()

    @app.get("/sneaky")
    async def s(items: tuple[_Sneaky, ...] = Query(default=(_Sneaky(1),))) -> dict:
        items[0].extra.append("x")
        return {"n": len(items[0].extra)}

    with TestClient(app) as client:
        assert [client.get("/sneaky").json()["n"] for _ in range(3)] == [1, 1, 1]


def test_a_frozen_dataclass_that_caches_into_its_instance_is_copied():
    """A `cached_property` writes straight into the instance dictionary, and
    what it will write is not there to inspect at registration."""
    app = Veloce()

    @app.get("/cached")
    async def c(items: tuple[_Cached, ...] = Query(default=(_Cached(1),))) -> dict:
        items[0].tally.append("x")
        return {"n": len(items[0].tally)}

    with TestClient(app) as client:
        assert [client.get("/cached").json()["n"] for _ in range(3)] == [1, 1, 1]


def test_a_datetime_carrying_a_mutable_tzinfo_is_copied_per_request():
    """An aware value reaches its `tzinfo`, which is an ordinary object."""
    app = Veloce()

    @app.get("/aware")
    async def a(
        items: tuple[datetime.datetime, ...] = Query(
            default=(datetime.datetime(2020, 1, 1, tzinfo=_MutableTz()),)
        ),
    ) -> dict:
        tz = items[0].tzinfo
        assert isinstance(tz, _MutableTz)
        tz.log.append("x")
        return {"n": len(tz.log)}

    with TestClient(app) as client:
        assert [client.get("/aware").json()["n"] for _ in range(3)] == [1, 1, 1]


def test_a_datetime_with_a_fixed_offset_is_still_shared():
    """The negative: `timezone.utc` has nothing to mutate, so the aware value
    is still handed over as one object."""
    app = Veloce()
    marker = Query(default=(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),))

    @app.get("/utc")
    async def u(items: tuple[datetime.datetime, ...] = marker) -> dict:
        return {"n": len(items)}

    with TestClient(app) as client:
        assert client.get("/utc").status_code == 200
    assert marker.default_factory is None


@pytest.mark.parametrize(
    "default",
    [_SlottedRows(["seed"]), _ChildOfSlotted(["seed"], "x")],
    ids=["slots-field", "inherited-slot"],
)
def test_a_list_a_frozen_dataclass_holds_in_a_slot_is_copied_per_request(default):
    """Reading either the instance dictionary or the declared fields - rather
    than both plus the slots - left a list reachable by every request: an
    unslotted child's dictionary does not mention a field it inherited from a
    slotted base."""
    app = Veloce()

    @app.get("/s")
    async def s(items: tuple[object, ...] = Query(default=(default,))) -> dict:
        items[0].rows.append("x")
        return {"n": len(items[0].rows)}

    with TestClient(app) as client:
        assert [client.get("/s").json()["n"] for _ in range(3)] == [2, 2, 2]


def test_a_list_in_a_non_field_slot_is_seen_even_though_it_cannot_be_copied():
    """A slot that is no field at all is reachable and is now read, so the
    value is no longer called immutable.

    It still cannot be isolated: `copy` restores slot state with `setattr`,
    which `frozen` refuses, so a hand-written `__slots__` makes the instance
    uncopyable - `@dataclass(frozen=True, slots=True)` generates the
    `__getstate__`/`__setstate__` pair that makes it work. An uncopyable
    default is shared as written, the same answer as a member holding a lock.
    """
    holder = _ExtraSlot(1)
    assert is_immutable(holder) is False
    assert deepcopyable(holder) is False


def _nest(depth: int) -> object:
    deep: object = ["leaf"]
    for _ in range(depth):
        deep = (deep,)
    return deep


def _copyable_depth() -> int:
    """A nesting depth a plain `copy.deepcopy` handles, with little to spare.

    Measured rather than pinned: the depth that fits depends on the recursion
    limit, on how many frames one copy level costs - which differs by
    interpreter version - and on the stack pytest has already spent. A pinned
    450 copied on 3.12 and overflowed on 3.11.
    """
    depth = 0
    while depth < 2000:
        try:
            copy.deepcopy(_nest(depth + 10))
        except RecursionError:
            break
        depth += 10
    assert depth, "no depth was copyable, so the margin cannot be the difference"
    return depth


def test_a_default_too_deep_to_copy_with_stack_to_spare_is_not_copied():
    """A copy attempted at registration has a shallower stack than the same
    copy inside a request, so a structure in between passed here and raised
    there - a 500 on every request that omitted the value."""
    deep = _nest(_copyable_depth())
    assert copy.deepcopy(deep) is not None, "the copy must pass at this depth to be the case"
    assert deepcopyable(deep) is False


def test_a_default_shallow_enough_to_copy_safely_is_still_copied():
    """The negative: the margin is headroom, not a lower bound on nesting."""
    nested: object = ["leaf"]
    for _ in range(50):
        nested = (nested,)
    assert deepcopyable(nested) is True


def test_a_deeply_nested_mutable_default_serves_rather_than_failing():
    app = Veloce()
    deep: object = ["leaf"]
    for _ in range(450):
        deep = (deep,)

    @app.get("/deep")
    async def d(v: tuple[object, ...] = Query(default=(deep,))) -> dict:
        return {"n": len(v)}

    with TestClient(app) as client:
        assert [client.get("/deep").status_code for _ in range(3)] == [200, 200, 200]


def test_a_list_in_a_private_slot_is_read_despite_the_mangled_name():
    """`__slots__ = ("__hidden",)` declared in `_Private` is stored as
    `_Private__hidden`, so reading the declared name read nothing at all and
    the list was handed to every request."""
    holder = _Private()
    assert holder.rows == []
    assert is_immutable(holder) is False


def test_an_attribute_whose_read_raises_does_not_stop_registration():
    """A property or `__getattr__` can raise anything, and a check that exists
    to keep requests safe must not be what stops a route being declared."""
    app = Veloce()

    @app.get("/lazy")
    async def lazy(items: tuple[_Lazy, ...] = Query(default=(_Lazy(1),))) -> dict:
        return {"n": len(items)}

    with TestClient(app) as client:
        assert client.get("/lazy").status_code == 200
    assert is_immutable(_Lazy(1)) is False


def test_an_unset_slot_is_not_mistaken_for_mutable_state():
    """The negative: an unset slot holds nothing to share, so it must not be
    the reason a value is copied."""
    assert is_immutable(_UnsetSlot(1)) is True


def test_a_mutable_default_is_still_copied_when_registration_runs_deep():
    """The copy trial holds frames in reserve so a copy that passes here also
    passes in the deeper frame a request runs in. Near the recursion limit
    there is no reserve to hold, and refusing the copy for that reason shared
    an ordinary mutable default instead of copying it."""

    def at_depth(remaining: int) -> bool:
        if remaining:
            return at_depth(remaining - 1)
        return deepcopyable(["a"])

    assert at_depth(900) is True


def test_a_slot_shadowed_by_a_subclass_attribute_is_still_read():
    """A class attribute of the same name wins the `getattr` lookup, which then
    answers with the class attribute while the slot holds the list that would
    be shared - so each slot is read through its own descriptor.

    Filling a shadowed slot takes the descriptor too, which is why this builds
    the instance rather than calling the dataclass constructor.
    """
    holder = _ShadowedSlot.__new__(_ShadowedSlot)
    _SLOT_DESCRIPTOR.__set__(holder, ["seed"])
    assert getattr(holder, "rows") == ()
    assert _SLOT_DESCRIPTOR.__get__(holder, _ShadowedSlot) == ["seed"]
    assert is_immutable(holder) is False
