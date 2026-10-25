"""A `Field` in `Annotated` is enforced without the plan knowing the template.

The fold used to pick `Path` or `Query` at plan build, which made the plan
route-dependent: the same handler needed a different plan under a different
prefix, and a merged route had to decide whether the one it inherited was still
valid. It also could not get the two apart - picking `Path` from the parameter
name while the marker's `lookup_name` took the `Field`'s alias made a path
parameter look itself up under the alias and 422.

So the `Field` rides on the bare path-or-query slot instead, as a source-less
marker, and the resolver validates after its own lookup has decided where the
value came from. The plan is handler-local again and the alias question does not
arise: the path reads the template's name, the query reads the alias.
"""

from __future__ import annotations

import warnings
from decimal import Decimal
from typing import Annotated

import pytest
from pydantic import AliasChoices, BaseModel, Field

from tests._openapi import document, parameter
from veloce import APIRouter, Body, Depends, Query, Veloce
from veloce._handler_plan import build_plan
from veloce._params import ParamBase, find_field_info
from veloce._ws_listener import _bounded_scalar
from veloce.contrib.mcp.plan_bridge import _coerce_argument, build_input_schema
from veloce.exceptions import RequestValidationError
from veloce.testclient import TestClient


class Item(BaseModel):
    name: str


def _app(template: str, annotation: object, name: str = "item_id", **kwargs: object) -> Veloce:
    app = Veloce()
    namespace: dict[str, object] = {}
    default = kwargs.get("default", ...)
    if default is not ...:
        source = f"async def handler({name}: ann = {default!r}):\n    return {{'v': {name}}}"
    else:
        source = f"async def handler({name}: ann):\n    return {{'v': {name}}}"
    namespace["ann"] = annotation
    exec(compile(source, "<handler>", "exec"), namespace)  # noqa: S102
    app.add_route(template, namespace["handler"], methods=["GET"])
    return app


def _client(template: str, annotation: object, **kwargs: object) -> TestClient:
    return TestClient(_app(template, annotation, **kwargs))


# ── the alias question the plan-time fold could not answer ──────────


def test_an_alias_on_a_path_parameter_still_binds_from_the_template():
    """The regression this task exists for: the template stores `item_id`."""
    response = _client("/items/{item_id}", Annotated[int, Field(alias="itemId", ge=1)]).get(
        "/items/5"
    )
    assert response.status_code == 200
    assert response.json() == {"v": 5}


def test_an_alias_on_a_query_parameter_names_the_wire_key():
    client = _client("/q", Annotated[int, Field(alias="itemId", ge=1)])
    assert client.get("/q?itemId=5").json() == {"v": 5}
    assert client.get("/q?item_id=5").status_code == 422


# ── the constraint is enforced from whichever source answered ───────


def test_a_path_parameter_constraint_is_enforced():
    client = _client("/items/{item_id}", Annotated[int, Field(ge=1)])
    assert client.get("/items/5").json() == {"v": 5}
    detail = client.get("/items/0").json()["detail"][0]
    assert detail["loc"] == ["path", "item_id"]


def test_a_query_parameter_constraint_is_enforced():
    client = _client("/q", Annotated[int, Field(ge=1)])
    assert client.get("/q?item_id=5").json() == {"v": 5}
    assert client.get("/q?item_id=0").json()["detail"][0]["loc"] == ["query", "item_id"]


def test_an_unconstrained_parameter_still_binds_from_either_source():
    """The negative: a parameter with no `Field` must resolve exactly as before."""
    assert _client("/items/{item_id}", int).get("/items/5").json() == {"v": 5}
    assert _client("/q", int).get("/q?item_id=5").json() == {"v": 5}


def test_a_list_parameter_bound_is_enforced():
    client = _client("/tags", Annotated[list[str], Field(max_length=2)])
    assert client.get("/tags?item_id=a&item_id=b").json() == {"v": ["a", "b"]}
    assert client.get("/tags?item_id=a&item_id=b&item_id=c").status_code == 422


# ── and published, for whichever location the route gives it ────────


def test_the_schema_publishes_the_constraint_for_both_locations():
    for template, location in (("/items/{item_id}", "path"), ("/q", "query")):
        app = _app(template, Annotated[int, Field(ge=1)])
        param = parameter(document(app), template, "item_id")
        assert param is not None, f"no parameter published for {template}"
        assert param["in"] == location
        assert param["schema"]["minimum"] == 1


# ── a Field that declares nothing a marker could carry ──────────────


def test_a_field_with_no_constraint_is_silent(recwarn):
    """`Field(description=...)` is a legitimate documentation-only spelling."""
    _app("/body", Annotated[Item, Field(description="doc only")], name="item")
    assert [str(w.message) for w in recwarn if "Field()" in str(w.message)] == []


def test_an_explicit_marker_still_wins_over_a_field():
    """The negative for that: a real conflict is still reported."""

    def handler(item_id: Annotated[int, Field(ge=1)] = Query(le=10)):
        return item_id

    with pytest.warns(UserWarning, match="the marker wins"):
        build_plan(handler)


# ── the plan is handler-local again ─────────────────────────────────


def test_an_overridden_dependency_enforces_its_field():
    """The old diagnostic existed because the fold could not name a source.

    There is no source to name now, so the constraint simply applies - and the
    override stops quietly dropping validation the real dependency performs.
    """

    def dep(limit: Annotated[int, Field(ge=1)]) -> int:
        return limit

    def override(limit: Annotated[int, Field(ge=1)]) -> int:
        return limit

    app = Veloce()

    @app.get("/x")
    async def route(value: int = Depends(dep)):
        return {"value": value}

    app.dependency_overrides[dep] = override
    client = TestClient(app)
    assert client.get("/x?limit=0").status_code == 422
    assert client.get("/x?limit=5").json() == {"value": 5}


def test_a_merged_route_reuses_the_source_plan():
    """Nothing re-plans, because nothing about the plan depends on the route."""
    router = APIRouter()

    @router.get("/items")
    async def items(tenant_id: Annotated[str, Field(min_length=1)]):
        return {"tenant": tenant_id}

    child = router.match("GET", "/items")
    assert child is not None
    app = Veloce()
    app.include_router(router, prefix="/tenants/{tenant_id}")
    merged = app.match("GET", "/tenants/acme/items")
    assert merged is not None
    assert merged.route_info.handler_plan is child.route_info.handler_plan
    assert TestClient(app).get("/tenants/acme/items").json() == {"tenant": "acme"}


# ── both doors publish and enforce the same thing ───────────────────


def test_an_aliased_query_parameter_is_published_under_the_name_it_is_served_under():
    """A document naming a key the route does not accept cannot be called."""
    app = _app("/q", Annotated[int, Field(alias="itemId", ge=1)])
    doc = document(app)
    names = [p["name"] for p in doc["paths"]["/q"]["get"].get("parameters", [])]
    assert names == ["itemId"]
    assert TestClient(app).get("/q?itemId=5").json() == {"v": 5}


def test_an_unaliased_parameter_is_still_published_under_its_own_name():
    """The negative: introducing the alias must not rename everything else."""
    app = _app("/q", Annotated[int, Field(ge=1)])
    names = [p["name"] for p in document(app)["paths"]["/q"]["get"].get("parameters", [])]
    assert names == ["item_id"]


def test_the_mcp_door_enforces_a_folded_bound():
    """Published-but-unenforced is the inverse of the defect this fold fixed.

    Goes through `_coerce_argument`, the door an agent's call actually takes -
    asserting on `slot.marker.validate` directly tests the marker, not the door,
    and passes even when the door ignores it.
    """

    async def tool(page: Annotated[int, Field(ge=10)]):
        return page

    slot = build_plan(tool).slots[0]
    assert _coerce_argument(slot, 10) == 10
    # `RequestValidationError` is what the tool boundary catches and turns into
    # a refused call; a bare ValueError escapes it as an internal error.
    with pytest.raises(RequestValidationError, match=">= 10"):
        _coerce_argument(slot, 1)


def test_the_mcp_door_enforces_a_folded_list_bound():
    async def tool(tags: Annotated[list[str], Field(max_length=1)]):
        return tags

    slot = build_plan(tool).slots[0]
    assert _coerce_argument(slot, ["a"]) == ["a"]
    with pytest.raises(RequestValidationError):
        _coerce_argument(slot, ["a", "b"])


def test_a_documentation_only_field_adds_no_per_request_check():
    """It still reaches the schema, but nothing is validated per request."""

    async def documented(q: Annotated[str, Field(description="search")] = ""):
        return q

    async def bounded(q: Annotated[str, Field(min_length=2)] = ""):
        return q

    doc_slot = build_plan(documented).slots[0]
    assert doc_slot.marker is not None, "the description must still reach the lowerings"
    assert doc_slot.marker_validates is False
    assert build_plan(bounded).slots[0].marker_validates is True


# ── the edges the fold newly made reachable ─────────────────────────


def test_a_body_array_bound_is_checked_after_coercion():
    """Both doors must agree: the query path coerces then validates.

    Checking the raw JSON first let `["0"]` past a `ge=1` bound, because
    `ParamBase.validate` takes the string branch and skips the numeric bounds -
    and the handler then received the coerced `0`, which the published
    `items.minimum` forbids.
    """
    app = Veloce()

    @app.post("/items")
    async def items(payload: list[int] = Body(ge=1)):
        return {"v": payload}

    client = TestClient(app)
    assert client.post("/items", json=[1]).json() == {"v": [1]}
    assert client.post("/items", json=["0"]).status_code == 422
    assert client.post("/items", json=[0]).status_code == 422


def test_a_validated_data_default_factory_is_refused_at_registration():
    """pydantic >= 2.10 allows a one-argument factory; `resolve_default` calls
    it with none, so accepting it means a 500 on every request that omits the
    parameter instead of a refusal the author sees immediately."""

    async def handler(q: Annotated[str, Field(default_factory=lambda data: "x")]):
        return q

    with pytest.raises(TypeError, match="default_factory"):
        build_plan(handler)


def test_a_zero_argument_default_factory_is_still_accepted():
    """The negative: the ordinary spelling must keep working."""

    async def handler(q: Annotated[str, Field(default_factory=lambda: "x")]):
        return q

    assert build_plan(handler).slots[0].marker.resolve_default() == "x"


def test_a_none_default_is_not_published_on_a_non_nullable_schema():
    """A schema whose own default violates its type is one a strict client
    rejects, and one an agent may echo back as `null` for the door to refuse."""

    async def tool(page: Annotated[int, Field(ge=1)] = None):
        return page

    page = build_input_schema(build_plan(tool), {})["properties"]["page"]
    assert page["minimum"] == 1
    assert "default" not in page, "a null default on a non-nullable schema"


def test_a_real_default_is_still_published():
    """The negative: skipping `None` must not stop publishing a usable default."""

    async def tool(page: Annotated[int, Field(ge=1)] = 3):
        return page

    page = build_input_schema(build_plan(tool), {})["properties"]["page"]
    assert page["default"] == 3


def test_an_unresolvable_annotation_carrying_a_bound_is_refused():
    """A dropped annotation now drops an enforceable bound with it.

    Before the fold the `Field` was ignored anyway, so losing the annotation
    cost nothing it declared. Now it does, which makes the declaration
    load-bearing: refused where the author can see it, rather than served with
    the bound silently gone.
    """

    async def handler(page: Annotated[NoSuchName, Field(ge=1)] = 1):  # noqa: F821
        return page

    with pytest.raises(TypeError, match="could not resolve"):
        build_plan(handler)


def test_an_unresolvable_annotation_carrying_no_bound_is_still_tolerated():
    """The negative: a documentation-only `Field` loses nothing enforceable."""

    async def handler(page: Annotated[NoSuchName, Field(description="x")] = 1):  # noqa: F821
        return page

    with pytest.warns(UserWarning, match="could not resolve"):
        build_plan(handler)


# ── negatives for the two fixes that had only a positive ────────────


def test_a_parameter_without_a_field_publishes_no_bounds():
    """The negative for the schema fix: the fold must not invent constraints."""
    for template in ("/items/{item_id}", "/q"):
        param = parameter(document(_app(template, int)), template, "item_id")
        assert param is not None
        assert set(param["schema"]) <= {"type"}, param["schema"]


def test_an_override_without_a_field_is_unaffected():
    """The negative for the override fix: no constraint, no refusal."""

    def dep(limit: int) -> int:
        return limit

    def override(limit: int) -> int:
        return limit

    app = Veloce()

    @app.get("/x")
    async def route(value: int = Depends(dep)):
        return {"value": value}

    app.dependency_overrides[dep] = override
    client = TestClient(app)
    assert client.get("/x?limit=0").json() == {"value": 0}
    assert client.get("/x?limit=5").json() == {"value": 5}


# ── round two: the same inversion, for an explicit list marker ──────


def test_the_mcp_door_enforces_an_explicit_list_markers_bounds():
    """The folded spelling was fixed last round; the explicit one published the
    same bounds and enforced none of them, which is the same inversion."""

    async def tool(tags: Annotated[list[int], Query(ge=1, min_length=2, max_length=3)]):
        return tags

    slot = build_plan(tool).slots[0]
    assert _coerce_argument(slot, [1, 2]) == [1, 2]
    for refused in ([0, 2], [1], [1, 2, 3, 4]):
        with pytest.raises(RequestValidationError):
            _coerce_argument(slot, refused)


def test_the_mcp_door_still_accepts_an_unbounded_list_marker():
    """The negative: a marker declaring no bound must not start refusing."""

    async def tool(tags: Annotated[list[int], Query()] = None):
        return tags

    slot = build_plan(tool).slots[0]
    assert _coerce_argument(slot, [1, 2, 3, 4, 5]) == [1, 2, 3, 4, 5]


def test_a_decimal_field_keyword_is_not_dropped_in_silence():
    """`max_digits` / `decimal_places` are pydantic's own keywords, not
    `annotated_types` classes, so the fold cannot carry them - but silently
    losing a declared bound is the defect the fold exists to stop."""

    async def handler(amount: Annotated[Decimal, Field(max_digits=4, decimal_places=2)] = None):
        return amount

    with pytest.warns(UserWarning, match="max_digits, decimal_places") as caught:
        build_plan(handler)
    assert "neither enforced nor published" in str(caught[0].message)


def test_a_carryable_constraint_does_not_warn_about_being_uncarried(recwarn):
    """The negative: `ge` IS carried, so nothing is lost and nothing is said."""

    async def handler(amount: Annotated[Decimal, Field(ge=1)] = None):
        return amount

    build_plan(handler)
    assert [w for w in recwarn if "cannot carry" in str(w.message)] == []


def test_a_multi_name_validation_alias_is_not_dropped_in_silence():
    """An `AliasChoices` names a lookup a marker has one string to express.

    The aliases are ignored and the parameter is served under its own name, so
    without this the declaration vanished and a caller using either alias got
    the default back with nothing said at registration.
    """

    async def handler(v: Annotated[int, Field(validation_alias=AliasChoices("a", "b"))] = 0):
        return v

    with pytest.warns(UserWarning, match="validation_alias") as caught:
        build_plan(handler)
    assert "neither enforced nor published" in str(caught[0].message)


def test_a_single_name_validation_alias_is_carried_without_complaint():
    """The negative: a plain string IS the wire key, so nothing is lost."""

    async def handler(v: Annotated[int, Field(validation_alias="qq")] = 0):
        return v

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_plan(handler)
    assert [w for w in caught if "validation_alias" in str(w.message)] == []


# ── round three: a frame is not an absent value ─────────────────────


def test_a_null_frame_is_refused_rather_than_defaulted():
    """`ParamBase.validate` substitutes the `Field`'s default for `None`, which
    is right for a parameter that can be absent and wrong for a frame: a frame
    either arrived or it did not. `Field(0, ge=0)` turned `null` into `0`."""
    marker = ParamBase.from_field_info(find_field_info(Annotated[int, Field(0, ge=0)]))
    with pytest.raises(ValueError, match="may not be null"):
        _bounded_scalar(marker, False, int, None)
    assert _bounded_scalar(marker, False, int, 5) == 5


def test_a_nullable_message_still_accepts_null():
    """The negative: `int | None` asked for `null`, so it keeps getting it."""
    marker = ParamBase.from_field_info(find_field_info(Annotated[int, Field(ge=1)]))
    assert _bounded_scalar(marker, True, int, None) is None


def test_a_nullable_parameter_still_publishes_its_null_default():
    """The round-one `None` guard over-applied: a nullable parameter's `null`
    default is valid against its own `anyOf` schema and is what a schema-aware
    client should read."""

    async def tool(x: int | None = Query(None, ge=1)):
        return x

    prop = build_input_schema(build_plan(tool), {})["properties"]["x"]
    assert "anyOf" in prop
    assert prop["default"] is None


def test_a_non_nullable_parameter_still_omits_a_null_default():
    """The negative it was added for: a schema whose default violates its type."""

    async def tool(x: Annotated[int, Field(ge=1)] = None):
        return x

    assert "default" not in build_input_schema(build_plan(tool), {})["properties"]["x"]


# ── round four: three of my own fixes were wrong or missing ─────────


def test_a_validation_alias_names_the_wire_key():
    """pydantic gives `validation_alias` precedence over `alias`; it was read
    under the Python name instead, with no diagnostic."""
    client = _client("/va", Annotated[int, Field(validation_alias="qq", ge=1)])
    assert client.get("/va?qq=5").json() == {"v": 5}
    assert client.get("/va?item_id=5").status_code == 422


def test_a_validation_alias_is_published_under_the_name_it_is_served_under():
    app = _app("/va", Annotated[int, Field(validation_alias="qq", ge=1)])
    names = [p["name"] for p in document(app)["paths"]["/va"]["get"].get("parameters", [])]
    assert names == ["qq"]


def test_a_plain_alias_is_unaffected():
    """The negative: `alias` alone keeps working as it did."""
    client = _client("/a", Annotated[int, Field(alias="aa", ge=1)])
    assert client.get("/a?aa=5").json() == {"v": 5}


def test_the_mcp_door_coerces_list_elements_before_bounding_them():
    """The trap `dependency.py` names: `["0"]` takes the string branch of
    `ParamBase.validate` and skips the numeric bound the schema publishes, and
    the handler then receives a `str` for a declared `list[int]`."""

    async def tool(t: list[int] = Query(ge=5, max_length=2)):
        return t

    slot = build_plan(tool).slots[0]
    assert _coerce_argument(slot, [5]) == [5]
    with pytest.raises(RequestValidationError):
        _coerce_argument(slot, [0])
    with pytest.raises(RequestValidationError):
        _coerce_argument(slot, ["0"])
    with pytest.raises(RequestValidationError):
        _coerce_argument(slot, ["abc"])


def test_a_registration_diagnostic_names_the_handler_it_came_from():
    """All of these land on one framework line in `router.py`, so without the
    handler's name an app registering many routes cannot tell which one.
    """

    async def charge_account(amount: Annotated[Decimal, Field(max_digits=4)] = None):
        return amount

    with pytest.warns(UserWarning) as caught:
        build_plan(charge_account)
    assert any("charge_account" in str(w.message) for w in caught), [str(w.message) for w in caught]


def test_the_diagnostic_still_names_the_parameter(recwarn):
    """The negative: naming the handler must not cost the parameter's name."""

    async def charge(amount: Annotated[Decimal, Field(max_digits=4)] = None):
        return amount

    with pytest.warns(UserWarning, match="'amount'"):
        build_plan(charge)


@pytest.mark.parametrize(
    "annotation",
    [
        int,
        Annotated[int, Field(ge=1)],
        Annotated[int, Field(alias="lim")],
        Annotated[int, Field(description="doc only")],
        Annotated[list[int], Field(min_length=1)],
        list[str],
    ],
    ids=["plain", "bounded", "aliased", "doc-only", "bounded-list", "plain-list"],
)
def test_every_path_or_query_slot_carries_a_lookup_name(annotation):
    """The resolvers read `slot.lookup_name` with no fallback, so it must be set.

    `_adopt_field_marker` sets it for both bare kinds before its own early
    return, which is why the `or name` the three read sites used to carry was
    dead. This pins the invariant: a new `K_QUERY` builder that skipped that
    call would otherwise look the parameter up under an empty string and report
    it missing, rather than failing loudly here.
    """

    async def handler(value=None):
        return value

    handler.__annotations__ = {"value": annotation, "return": dict}
    slots = [s for s in build_plan(handler).slots if s.name == "value"]
    assert slots, "the parameter produced no slot"
    for slot in slots:
        assert slot.lookup_name, f"kind {slot.kind} carries no lookup_name"


# ── a default declared beside the marker ─────────────────────────────
#
# `docs/guide/parameters.md` calls this the way to keep "the argument's real
# default free for an ordinary value": the marker in the `Annotated` metadata
# supplies the constraints, the value after `=` supplies the default. Hoisting
# the marker into the default position overwrote that value, so the parameter
# had no default at all and the documented example answered 422.


def test_the_documented_annotated_default_is_honoured():
    app = Veloce()

    @app.get("/search")
    async def search(q: Annotated[str, Query(max_length=50)] = "") -> dict:
        return {"q": q}

    with TestClient(app) as client:
        assert client.get("/search").status_code == 200
        assert client.get("/search").json() == {"q": ""}
        assert client.get("/search?q=hi").json() == {"q": "hi"}


def test_an_annotated_default_still_validates_what_is_sent():
    """The negative: the default is not a way past the marker's constraints."""
    app = Veloce()

    @app.get("/search")
    async def search(q: Annotated[str, Query(max_length=3)] = "") -> dict:
        return {"q": q}

    with TestClient(app) as client:
        assert client.get("/search").status_code == 200
        assert client.get("/search?q=toolong").status_code == 422


def test_a_mutable_annotated_default_is_not_shared_between_requests():
    """Folding the default into the marker applies the copying factory a
    declared mutable default already gets."""
    app = Veloce()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        @app.get("/tags")
        async def tags(t: Annotated[list[str], Query()] = ["x"]) -> dict:
            t.append("own")
            return {"t": t}

    with TestClient(app) as client:
        assert [client.get("/tags").json()["t"] for _ in range(3)] == [["x", "own"]] * 3


#: Module scope because this module uses `from __future__ import annotations`:
#: a marker built inside a test function could never be resolved from the
#: string annotation that references it.
_SHARED_MARKER = Query(max_length=50)


def test_a_marker_shared_by_two_parameters_keeps_each_default():
    """A copy, not the marker itself: one marker can be declared once and used
    twice, and the first parameter's default must not become the second's."""
    app = Veloce()

    @app.get("/two")
    async def two(
        a: Annotated[str, _SHARED_MARKER] = "a",
        b: Annotated[str, _SHARED_MARKER] = "b",
    ) -> dict:
        return {"a": a, "b": b}

    with TestClient(app) as client:
        assert client.get("/two").json() == {"a": "a", "b": "b"}
    assert _SHARED_MARKER.has_default is False


def test_a_marker_declaring_its_own_default_keeps_it():
    """The marker's own default is the more explicit statement, so a signature
    default beside it does not displace it."""
    app = Veloce()

    @app.get("/limit")
    async def limit(n: Annotated[int, Query(default=5)] = 10) -> dict:
        return {"n": n}

    with TestClient(app) as client:
        assert client.get("/limit").json() == {"n": 5}
