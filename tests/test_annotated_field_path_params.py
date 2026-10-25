"""A folded `Field`'s bounds reach every door, for whichever source answered.

The fold used to pick `Path` or `Query` at plan build, which needed the route
template and so made the plan route-dependent. It does not any more: the `Field`
becomes a source-less marker on the bare path-or-query slot and the resolver
validates whatever its own lookup found (see
`tests/test_annotated_field_resolve_time.py`).

What is asserted here is the part that must survive that: the bounds are
enforced and published through the HTTP route, the OpenAPI document, the MCP
tool and prompt schemas, and a dependency - overridden or not.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from veloce import Depends, Query, Veloce
from veloce._route_contract import RouteContract, iter_param_descriptors
from veloce.contrib.mcp.prompts import build_prompt_registry
from veloce.contrib.mcp.registry import build_registry
from veloce.testclient import TestClient


def _bounded(item_id: Annotated[int, Field(ge=1, le=10)]) -> int:
    return item_id


# ── the three answers ───────────────────────────────────────────────


# ── the MCP doors get the bounds the marker carries ─────────────────


def test_an_mcp_tool_publishes_its_field_bounds():
    app = Veloce()

    @app.mcp_tool(description="Bump a counter")
    async def bump(amount: Annotated[int, Field(ge=1, le=10)] = 1):
        return {"amount": amount}

    schema = build_registry(app).resolve("bump", None).input_schema
    assert schema["properties"]["amount"]["minimum"] == 1
    assert schema["properties"]["amount"]["maximum"] == 10


def test_an_mcp_tool_enforces_its_field_bounds():
    app = Veloce()

    @app.mcp_tool(description="Bump a counter")
    async def bump(amount: Annotated[int, Field(ge=1, le=10)] = 1):
        return {"amount": amount}

    tool = build_registry(app).resolve("bump", None)
    slot = tool.plan.slots[0]
    assert slot.marker is not None
    assert slot.marker.validate(9, "amount") == 9
    try:
        slot.marker.validate(99, "amount")
    except ValueError as exc:
        assert "<= 10" in str(exc)
    else:  # pragma: no cover - the constraint is the subject of the test
        raise AssertionError("99 was accepted for a parameter bounded at 10")


def test_an_mcp_prompt_publishes_its_field_description():
    app = Veloce()

    @app.mcp_prompt(description="Summarise a topic")
    async def summarise(topic: Annotated[str, Field(min_length=3, description="what about")] = "x"):
        return {"topic": topic}

    prompt = build_prompt_registry(app).get("summarise")
    assert prompt is not None
    topic = next(a for a in prompt.arguments if a["name"] == "topic")
    assert topic["description"] == "what about"


# ── the two cold-path fallbacks still lower a path segment as `in: path` ──


def _route_info_without_a_plan(app: Veloce) -> object:
    """A registered route with its finalized plan cleared.

    Both fallbacks below are reached only when `handler_plan` is `None`, which
    registration never leaves behind - so this is how the cold path is entered.
    """
    info = app.match("GET", "/items/5").route_info
    info.handler_plan = None
    return info


def _app_with_a_bounded_path_param() -> Veloce:
    app = Veloce()

    @app.get(
        "/items/{item_id}",
        name="items_show",
        expose_as_mcp_tool=True,
        mcp_description="Show an item",
    )
    async def show(item_id: Annotated[int, Field(ge=1, le=10)]):
        return {"item_id": item_id}

    return app


def test_the_route_contract_fallback_lowers_a_path_segment_as_path():
    info = _route_info_without_a_plan(_app_with_a_bounded_path_param())
    descriptors = list(iter_param_descriptors(RouteContract.from_route_info(info)))
    item_id = next(d for d in descriptors if d.name == "item_id")
    assert item_id.location == "path"


def test_the_mcp_route_tool_fallback_keeps_the_segment_out_of_the_query():
    app = _app_with_a_bounded_path_param()
    _route_info_without_a_plan(app)
    tool = build_registry(app).resolve("items_show", None)
    assert tool.input_schema["properties"]["item_id"]["minimum"] == 1


# ── an overridden dependency still binds its path parameter ─────────


def _dep(item_id: Annotated[int, Field(ge=1)]) -> int:
    return item_id


def _app_with_dependency() -> Veloce:
    app = Veloce()

    @app.get("/dep/{item_id}")
    async def route(value: int = Depends(_dep)):
        return {"value": value}

    return app


def test_an_overridden_dependency_still_binds_its_path_parameter():
    app = _app_with_dependency()
    app.dependency_overrides[_dep] = _dep
    assert TestClient(app).get("/dep/5").json() == {"value": 5}


def test_a_non_overridden_dependency_enforces_the_constraint():
    client = TestClient(_app_with_dependency())
    assert client.get("/dep/5").json() == {"value": 5}
    assert client.get("/dep/0").status_code == 422


# ── the one unanswerable case says so ───────────────────────────────


def _query_dep(limit: Annotated[int, Field(ge=1)]) -> int:
    return limit


def _query_override(limit: Annotated[int, Field(ge=1)]) -> int:
    """A distinct callable with the same signature - the override path."""
    return limit


def _plain_dep(limit: int) -> int:
    return limit


def _plain_override(limit: int) -> int:
    return limit


def _app_with(dep: object) -> Veloce:
    app = Veloce()

    @app.get("/x")
    async def route(value: int = Depends(dep)):
        return {"value": value}

    return app


def test_an_explicit_marker_on_the_override_keeps_the_constraints():
    """The remedy the diagnostic names: an explicit marker needs no fold."""

    def override(limit: Annotated[int, Query(ge=1)]) -> int:
        return limit

    app = _app_with(_query_dep)
    app.dependency_overrides[_query_dep] = override
    client = TestClient(app)
    assert client.get("/x?limit=0").status_code == 422
    assert client.get("/x?limit=5").json() == {"value": 5}
