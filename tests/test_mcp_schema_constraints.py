"""A tool schema publishes the constraints the runtime enforces.

`_slot_schema` copied a marker's `description`, `title` and `default` onto the
published property and stopped there. Every validation keyword was dropped, so
the tool schema an agent reads advertised a wider contract than the server
accepts:

    OpenAPI  {"type":"integer","minimum":1,"maximum":100,"default":5}
    MCP      {"type":"integer","default":5}

    tools/call {"limit": 999}  ->  isError: "limit must be <= 100"

An agent that reads schemas — the only kind worth publishing one for — is told
999 is allowed and is then refused for sending it. It has no way to correct
itself except by guessing, because the bound it violated was never published.

This is the same shape as the grouped-model case fixed earlier in this review:
the published contract laxer than the enforcement. That fix routed groups through
the model's own JSON Schema; this one routes plain markers through
`_apply_marker_constraints`, the function the OpenAPI lowering already uses. One
definition, both doors.

`description` and `title` were hand-copied here and are in that helper too, so
the duplication goes with it.
"""

from __future__ import annotations

import pathlib

import pytest
from pydantic import BaseModel, Field

from veloce import Cookie, Header, Query, Veloce
from veloce.testclient import TestClient


class Filters(BaseModel):
    """Module scope: this file uses PEP 563, so a local class cannot resolve."""

    limit: int = Field(10, ge=1, le=100)


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}


def _schema_for(marker, annotation=int, name="value"):
    """The published MCP property schema for one marker."""
    app = Veloce(title="C", version="1.0.0", openapi_url=None)

    async def handler(**kwargs) -> dict:
        return {}

    async def tool(value=marker) -> dict:
        return {"value": value}

    tool.__annotations__ = {"value": annotation, "return": dict}
    tool.__name__ = "probe"
    app.get("/probe", expose_as_mcp_tool=True, mcp_description="Probe")(tool)
    app.mount_mcp(transport="http", path="/mcp")

    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    listed = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json"},
    ).json()
    return listed["result"]["tools"][0]["inputSchema"]["properties"]["value"]


def _openapi_schema_for(marker, annotation=int):
    app = Veloce(title="C", version="1.0.0")

    async def tool(value=marker) -> dict:
        return {}

    tool.__annotations__ = {"value": annotation, "return": dict}
    tool.__name__ = "probe"
    app.get("/probe")(tool)
    return app.openapi()["paths"]["/probe"]["get"]["parameters"][0]["schema"]


# ── every constraint keyword reaches the tool schema ─────────────────


@pytest.mark.parametrize(
    ("marker", "annotation", "keyword", "expected"),
    [
        (Query(default=5, ge=1), int, "minimum", 1),
        (Query(default=5, le=100), int, "maximum", 100),
        (Query(default=5, gt=0), int, "exclusiveMinimum", 0),
        (Query(default=5, lt=50), int, "exclusiveMaximum", 50),
        (Query(default="", min_length=2), str, "minLength", 2),
        (Query(default="", max_length=8), str, "maxLength", 8),
        (Query(default=4, multiple_of=2), int, "multipleOf", 2),
        (Query(default="", regex="^a+$"), str, "pattern", "^a+$"),
    ],
)
def test_a_constraint_reaches_the_tool_schema(marker, annotation, keyword, expected):
    """The defect: every one of these was dropped."""
    assert _schema_for(marker, annotation)[keyword] == expected


@pytest.mark.parametrize(
    ("marker", "annotation"),
    [
        (Query(default=5, ge=1, le=100), int),
        (Query(default="", min_length=2, max_length=8), str),
        (Query(default=4, gt=0, lt=50, multiple_of=2), int),
    ],
)
def test_the_two_doors_publish_the_same_schema(marker, annotation):
    """The property: an agent and a browser are told the same contract."""
    assert _schema_for(marker, annotation) == _openapi_schema_for(marker, annotation)


def test_several_constraints_appear_together():
    schema = _schema_for(Query(default=5, ge=1, le=100), int)
    assert schema["minimum"] == 1
    assert schema["maximum"] == 100
    assert schema["type"] == "integer"
    assert schema["default"] == 5


@pytest.mark.parametrize("marker_kind", [Query, Header, Cookie])
def test_every_marker_location_carries_its_constraints(marker_kind):
    assert _schema_for(marker_kind(default=5, ge=1, le=100), int)["maximum"] == 100


# ── the metadata that already worked still does ──────────────────────


def test_a_description_still_reaches_the_schema():
    assert _schema_for(Query(default=5, description="How many"), int)["description"] == "How many"


def test_a_title_still_reaches_the_schema():
    assert _schema_for(Query(default=5, title="Limit"), int)["title"] == "Limit"


def test_a_default_still_reaches_the_schema():
    assert _schema_for(Query(default=5), int)["default"] == 5


def test_examples_reach_the_schema():
    assert _schema_for(Query(default=5, examples=[1, 2]), int)["examples"] == [1, 2]


def test_a_marker_with_no_constraints_publishes_none():
    """The negative: the fix must not invent keywords."""
    schema = _schema_for(Query(default=5), int)
    assert set(schema) == {"type", "default"}


def test_a_parameter_with_no_marker_is_unchanged():
    app = Veloce(title="C", version="1.0.0", openapi_url=None)

    @app.get("/probe", expose_as_mcp_tool=True, mcp_description="Probe")
    async def probe(value: int = 5) -> dict:
        return {"value": value}

    app.mount_mcp(transport="http", path="/mcp")
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    listed = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json"},
    ).json()
    schema = listed["result"]["tools"][0]["inputSchema"]["properties"]["value"]
    assert schema == {"type": "integer", "default": 5}


# ── the published bound is the one enforced ──────────────────────────


def _call(client: TestClient, arguments: dict) -> dict:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "probe", "arguments": arguments},
        },
        headers={"Accept": "application/json"},
    ).json()["result"]


def _bounded_client() -> TestClient:
    app = Veloce(title="C", version="1.0.0", openapi_url=None)

    @app.get("/probe", expose_as_mcp_tool=True, mcp_description="Probe")
    async def probe(limit: int = Query(default=5, ge=1, le=100)) -> dict:
        return {"limit": limit}

    app.mount_mcp(transport="http", path="/mcp")
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    return client


def test_a_value_inside_the_published_bound_is_accepted():
    assert _call(_bounded_client(), {"limit": 100})["content"][0]["text"] == '{"limit":100}'


def test_a_value_outside_the_published_bound_is_refused():
    """It always was; the point is that the bound is now published."""
    assert _call(_bounded_client(), {"limit": 999})["isError"] is True


def test_the_published_maximum_is_the_enforced_maximum():
    """The property this whole fix is about."""
    client = _bounded_client()
    listed = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json"},
    ).json()
    published = listed["result"]["tools"][0]["inputSchema"]["properties"]["limit"]["maximum"]
    assert _call(client, {"limit": published}).get("isError") is not True
    assert _call(client, {"limit": published + 1})["isError"] is True


# ── an optional parameter keeps its null branch ──────────────────────


def test_an_optional_constrained_parameter_still_accepts_null():
    """`_with_null_branch` must wrap the constraints, not sit beside them."""
    schema = _schema_for(Query(default=None, ge=1, le=100), int | None)
    assert "anyOf" in schema
    branches = schema["anyOf"]
    assert {"type": "null"} in branches
    constrained = [b for b in branches if b.get("type") == "integer"]
    assert constrained and constrained[0]["maximum"] == 100


def test_a_model_property_is_not_given_marker_constraints():
    """A `$ref` must not be decorated; the model carries its own schema."""
    app = Veloce(title="C", version="1.0.0", openapi_url=None)

    @app.get("/probe", expose_as_mcp_tool=True, mcp_description="Probe")
    async def probe(filters: Filters = Query(group=True)) -> dict:
        return {}

    app.mount_mcp(transport="http", path="/mcp")
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    listed = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json"},
    ).json()
    schema = listed["result"]["tools"][0]["inputSchema"]["properties"]["limit"]
    # From the model's own schema, which already carries the bounds.
    assert schema["maximum"] == 100


# ── one definition, not two ──────────────────────────────────────────


def test_the_bridge_uses_the_shared_constraint_helper():
    """Hand-copying `description` and `title` is how the drift started."""
    source = (
        pathlib.Path(__file__).resolve().parents[1] / "src/veloce/contrib/mcp/plan_bridge.py"
    ).read_text(encoding="utf-8")
    assert "_apply_marker_constraints" in source
    assert 'prop = {**prop, "description": d.marker.description}' not in source
