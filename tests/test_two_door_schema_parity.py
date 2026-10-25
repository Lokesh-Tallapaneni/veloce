"""One declaration, two lowerings, one description.

A route's parameters are published twice: to OpenAPI for a browser client and to
the MCP `inputSchema` for an agent. Both read the same slots, so a declaration
that reaches them described two different ways is a defect in whichever one
drifted - and five of those were found by review in a single sitting:

- a bounded list's `min_length` enforced on one door and ignored on the other
- the `K_QUERY_LIST` arm fixed for a null member while `K_PARAM_MARKER` was not
- `list_checks` gated on one of two sibling slot builders
- `default: null` guarded against a non-nullable schema on MCP but not OpenAPI
- `Field(default=(1, 2))` on a `tuple` published by MCP, dropped by OpenAPI

Each was caught by a reader noticing, which does not scale and did not catch the
last one until the rule was moved into `_publishable_default`. This module
catches the class instead: it enumerates declarations and asserts the two doors
agree about each, so a new divergence fails here rather than waiting for review.

**What must agree** is the type and every published constraint. **What may
differ** is listed in `DIVERGENT_KEYS` with the reason, because the two schema
dialects are not identical: MCP carries optionality as an `anyOf` null branch
and so says `default: null` where it is informative, while an OpenAPI parameter
schema is flat and says it with `required` plus the default key. A difference
outside that list is a bug, and adding to the list is a deliberate act.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import pytest
from pydantic import Field

from veloce import Body, Query, Veloce
from veloce._handler_plan import build_plan
from veloce.contrib.mcp.plan_bridge import build_input_schema
from veloce.testclient import TestClient

# Keys the two dialects are allowed to disagree on. Everything else must match.
DIVERGENT_KEYS = frozenset(
    {
        # MCP publishes optionality as an `anyOf` null branch; an OpenAPI
        # parameter schema is flat and carries it on `required` instead.
        "anyOf",
        "default",
        # OpenAPI names the enclosing parameter, not the schema.
        "title",
    }
)

# Each case: a label, and the annotation to publish through both doors.
CASES = [
    pytest.param(Annotated[int, Field(ge=1, le=10)], id="scalar-bound"),
    pytest.param(Annotated[str, Field(pattern=r"^a+$")], id="scalar-pattern"),
    pytest.param(Annotated[str, Field(min_length=2, max_length=5)], id="scalar-length"),
    pytest.param(Annotated[list[int], Field(min_length=2, max_length=4)], id="list-item-count"),
    pytest.param(Annotated[list[int], Field(ge=0, le=9)], id="list-element-bound"),
    pytest.param(Annotated[list[int], Field(min_length=1, ge=0)], id="list-both"),
    pytest.param(Annotated[int, Field(multiple_of=3)], id="multiple-of"),
    pytest.param(Annotated[list[int], Query(ge=0, min_length=1)], id="explicit-query-marker"),
    pytest.param(Annotated[list[str], Body(min_length=1)], id="explicit-body-marker"),
    pytest.param(Annotated[int | None, Field(ge=1)], id="nullable-scalar"),
    pytest.param(Annotated[list[int | None], Field(ge=0)], id="nullable-members"),
    pytest.param(Annotated[Decimal, Field(ge=1)], id="decimal"),
    pytest.param(Annotated[int, Field(description="just docs")], id="doc-only"),
    pytest.param(
        Annotated[str, Field(alias="lim", min_length=3)],
        id="aliased",
        marks=pytest.mark.xfail(
            strict=True,
            reason=(
                "Known, filed: HTTP publishes and reads the alias while MCP keys the "
                "argument on the Python name. Each door is self-consistent, so which "
                "name wins is a decision, not a crash - tracked as Task 5 in "
                "internal/plans/2026-10-22-filed-followups.md. Strict, so closing it "
                "fails here until this marker is removed."
            ),
        ),
    ),
]


def _flatten_nullable(schema: dict) -> dict:
    """Collapse an `anyOf` whose branches are one type plus null to that type.

    The dialects spell optionality differently - MCP as an `anyOf` null branch,
    an OpenAPI parameter as a flat schema plus `required: false` - so comparing
    them literally would report every nullable parameter as a divergence. The
    constraints live on the non-null branch either way, and those are what must
    agree.
    """
    branches = schema.get("anyOf")
    if not branches:
        return schema
    real = [b for b in branches if b.get("type") != "null"]
    if len(real) != 1:
        return schema
    merged = {k: v for k, v in schema.items() if k != "anyOf"}
    return {**real[0], **merged}


def _both_doors(annotation: object) -> tuple[dict, dict]:
    """Publish one declaration through both lowerings and return both schemas."""
    app = Veloce(title="Parity", version="1.0.0")

    async def handler(value=None) -> dict:
        return {"value": value}

    # Assigned rather than written in the signature: this module uses PEP 563,
    # so a local name in an annotation would not resolve.
    handler.__annotations__ = {"value": annotation, "return": dict}
    app.get("/p", expose_as_mcp_tool=True, mcp_description="Parity")(handler)

    mcp_props = build_input_schema(build_plan(handler), {})["properties"]
    ((mcp_name, mcp_schema),) = mcp_props.items()

    operation = TestClient(app).get("/openapi.json").json()["paths"]["/p"]["get"]
    params = operation.get("parameters") or []
    if params:
        ((http_name, http_schema),) = ((p["name"], p["schema"]) for p in params)
    else:
        # A body-marked parameter is published as the request body rather than
        # in `parameters`; MCP has one argument slot either way.
        body = operation["requestBody"]["content"]["application/json"]
        http_name, http_schema = mcp_name, body["schema"]

    assert mcp_name == http_name, f"wire name differs: MCP {mcp_name!r} vs HTTP {http_name!r}"
    return _flatten_nullable(mcp_schema), _flatten_nullable(http_schema)


@pytest.mark.parametrize("annotation", CASES)
def test_both_doors_publish_the_same_constraints(annotation):
    """Every key the two dialects share must carry the same value."""
    mcp_schema, http_schema = _both_doors(annotation)
    shared = (set(mcp_schema) & set(http_schema)) - DIVERGENT_KEYS
    for key in sorted(shared):
        assert mcp_schema[key] == http_schema[key], (
            f"{key!r} differs - MCP {mcp_schema[key]!r} vs HTTP {http_schema[key]!r}"
        )


@pytest.mark.parametrize("annotation", CASES)
def test_neither_door_publishes_a_constraint_the_other_omits(annotation):
    """A bound on one door and absent on the other is the defect this catches.

    `Field(default=(1, 2))` on a `tuple` was published by MCP and dropped by
    OpenAPI for exactly this reason, and nothing failed.
    """
    mcp_schema, http_schema = _both_doors(annotation)
    only_mcp = set(mcp_schema) - set(http_schema) - DIVERGENT_KEYS
    only_http = set(http_schema) - set(mcp_schema) - DIVERGENT_KEYS
    assert not only_mcp, f"MCP publishes {sorted(only_mcp)}, OpenAPI does not"
    assert not only_http, f"OpenAPI publishes {sorted(only_http)}, MCP does not"


def test_a_tuple_default_is_published_by_both_doors():
    """The regression that motivated the shared rule.

    An immutable non-scalar default escapes the copying factory a mutable one
    gets, so it reached the publishing rule - which the OpenAPI copy filtered
    to scalars and the MCP copy did not.
    """
    mcp_schema, http_schema = _both_doors(Annotated[tuple, Field(default=(1, 2))])
    assert mcp_schema.get("default") == [1, 2]
    assert http_schema.get("default") == [1, 2]


def test_a_null_default_on_a_non_nullable_parameter_reaches_neither_door():
    """A default must validate against its own schema (OpenAPI 3.1).

    A typed generator reading `{"type": "integer", "default": null}` emits an
    initialiser its own type rejects.
    """
    mcp_schema, http_schema = _both_doors(Annotated[int, Field(None, ge=1)])
    assert "default" not in mcp_schema
    assert "default" not in http_schema


def test_a_factory_default_reaches_neither_door():
    """A per-call value has no single value to publish."""
    mcp_schema, http_schema = _both_doors(
        Annotated[list[int], Field(default_factory=list, min_length=0)]
    )
    assert "default" not in mcp_schema
    assert "default" not in http_schema


def test_an_optional_parameter_gets_no_null_default_from_the_http_door():
    """OpenAPI unwraps `Optional[T]` to T's schema with no null branch.

    So `default: null` is as invalid there as on a non-nullable parameter -
    `required: false` is what carries the optionality. MCP publishes it because
    its schema does have the `anyOf` null branch to validate against, which is
    why the two doors state this one policy separately.
    """
    mcp_schema, http_schema = _both_doors(Annotated[int | None, Field(ge=1)])
    assert "default" not in http_schema
    assert http_schema["type"] == "integer"
