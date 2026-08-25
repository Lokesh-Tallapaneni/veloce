"""`payload: Payload = Body()` validates the model, like the bare form.

Two ways to declare a JSON body model. Only one of them worked:

    async def bare(payload: Payload):            -> Payload  validated
    async def marked(payload: Payload = Body()): -> dict     NOT validated

The marked form handed the handler the raw decoded `dict`, so the first attribute
access raised `AttributeError` and the route answered `500`:

    POST /marked {"note": "hi"}  ->  500 {"detail": "Internal Server Error"}

and a body that did not match the model at all was accepted just as happily,
because nothing checked it.

The cause: a `Body()` marker builds a `K_PARAM_MARKER` slot, whose resolver runs
`_coerce_scalar` — a *scalar* coercer, which passes a `dict` straight through —
and then the marker's own constraint check. Nothing on that path knew the target
was a model. The bare form builds a `K_BODY_MODEL` slot and validates.

`_resolve_pydantic_body`'s own comment states the intended contract: it prefixes
errors with `"body"` "so a single body model's errors carry the same
`["body", ...]` location as a `Body(...)` marker param". The two were always meant
to agree.

The MCP door already agreed with the bare form — `plan_bridge` validates a
`MK_BODY` marker whose target is a model — so the same route answered differently
depending on which door it came in by.

The model is now recognised when the plan is built, so the request path pays one
attribute test rather than an introspection call.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from veloce import Body, Veloce
from veloce.testclient import TestClient


class Payload(BaseModel):
    note: str
    count: int = 1


class Constrained(BaseModel):
    limit: int = Field(ge=1, le=100)


class Nested(BaseModel):
    inner: Payload


def _app() -> TestClient:
    app = Veloce(title="Body", version="1.0.0")

    @app.post("/bare")
    async def bare(payload: Payload) -> dict:
        return {"note": payload.note, "count": payload.count}

    @app.post("/marked")
    async def marked(payload: Payload = Body()) -> dict:
        return {"note": payload.note, "count": payload.count}

    @app.post("/embedded")
    async def embedded(payload: Payload = Body(embed=True)) -> dict:
        return {"note": payload.note}

    @app.post("/constrained")
    async def constrained(payload: Constrained = Body()) -> dict:
        return {"limit": payload.limit}

    @app.post("/nested")
    async def nested(payload: Nested = Body()) -> dict:
        return {"note": payload.inner.note}

    @app.post("/optional")
    async def optional(payload: Payload | None = Body(default=None)) -> dict:
        return {"got": payload.note if payload else None}

    return TestClient(app)


# ── the marked form validates ────────────────────────────────────────


def test_a_marked_body_model_is_validated():
    """The defect: the handler received a raw dict and raised."""
    assert _app().post("/marked", json={"note": "hi"}).json() == {"note": "hi", "count": 1}


def test_the_two_forms_agree():
    """The property: how the body was declared must not decide this."""
    client = _app()
    body = {"note": "hi", "count": 4}
    assert client.post("/bare", json=body).json() == client.post("/marked", json=body).json()


def test_a_marked_body_applies_model_defaults():
    assert _app().post("/marked", json={"note": "hi"}).json()["count"] == 1


def test_a_marked_body_coerces_a_field():
    """`"4"` becomes `4` because the model says `int`."""
    assert _app().post("/marked", json={"note": "hi", "count": "4"}).json()["count"] == 4


def test_a_nested_model_is_validated_too():
    assert _app().post("/nested", json={"inner": {"note": "deep"}}).json() == {"note": "deep"}


# ── an invalid body is refused, with the documented shape ────────────


def test_a_missing_required_field_is_refused():
    """The defect: this was accepted and reached the handler."""
    assert _app().post("/marked", json={"count": 2}).status_code == 422


def test_a_wrong_type_is_refused():
    assert _app().post("/marked", json={"note": "hi", "count": "not-a-number"}).status_code == 422


def test_a_non_object_body_is_refused():
    assert _app().post("/marked", json=["not", "an", "object"]).status_code == 422


def test_the_error_is_located_under_body():
    """The location `_resolve_pydantic_body`'s comment says both forms share."""
    detail = _app().post("/marked", json={"count": 2}).json()["detail"]
    assert detail[0]["loc"][0] == "body"
    assert "note" in detail[0]["loc"]


def test_both_forms_report_the_same_error_location():
    client = _app()
    bare = client.post("/bare", json={"count": 2}).json()["detail"]
    marked = client.post("/marked", json={"count": 2}).json()["detail"]
    assert [e["loc"] for e in bare] == [e["loc"] for e in marked]


def test_a_model_constraint_is_enforced():
    client = _app()
    assert client.post("/constrained", json={"limit": 50}).json() == {"limit": 50}
    assert client.post("/constrained", json={"limit": 999}).status_code == 422


# ── the marker's own options still work ──────────────────────────────


def test_embed_still_takes_the_value_from_under_the_name():
    """`Body(embed=True)` must keep extracting before validating."""
    assert _app().post("/embedded", json={"payload": {"note": "hi"}}).json() == {"note": "hi"}


def test_an_embedded_body_is_still_validated():
    assert _app().post("/embedded", json={"payload": {"count": 2}}).status_code == 422


def test_a_default_is_still_used_when_the_body_is_absent():
    assert _app().post("/optional", json=None).json() == {"got": None}


def test_an_optional_model_still_validates_when_present():
    client = _app()
    assert client.post("/optional", json={"note": "hi"}).json() == {"got": "hi"}
    assert client.post("/optional", json={"count": 2}).status_code == 422


# ── a scalar Body() is untouched ─────────────────────────────────────


def test_a_scalar_body_still_binds():
    """The path that always worked; the fix must not disturb it."""
    app = Veloce(openapi_url=None)

    @app.post("/s")
    async def s(note: str = Body(embed=True)) -> dict:
        return {"note": note}

    assert TestClient(app).post("/s", json={"note": "hi"}).json() == {"note": "hi"}


def test_a_scalar_body_constraint_still_applies():
    app = Veloce(openapi_url=None)

    @app.post("/s")
    async def s(count: int = Body(embed=True, ge=1, le=10)) -> dict:
        return {"count": count}

    client = TestClient(app)
    assert client.post("/s", json={"count": 5}).json() == {"count": 5}
    assert client.post("/s", json={"count": 99}).status_code == 422


def test_a_whole_body_scalar_still_binds():
    app = Veloce(openapi_url=None)

    @app.post("/s")
    async def s(note: str = Body()) -> dict:
        return {"note": note}

    assert TestClient(app).post("/s", json="hi").json() == {"note": "hi"}


# ── the content-type policy is unchanged ─────────────────────────────


def test_a_non_json_content_type_is_still_refused():
    """The CSRF guard on the bare form must reach the marked one too."""
    client = _app()
    response = client.post(
        "/marked", content=b'{"note": "hi"}', headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 422


def test_malformed_json_is_still_a_400():
    client = _app()
    response = client.post(
        "/marked", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400


# ── the other doors agree ────────────────────────────────────────────


def test_the_openapi_document_describes_the_model():
    app = Veloce(title="Body", version="1.0.0")

    @app.post("/marked")
    async def marked(payload: Payload = Body()) -> dict:
        return {}

    body = app.openapi()["paths"]["/marked"]["post"]["requestBody"]
    schema = body["content"]["application/json"]["schema"]
    assert "$ref" in str(schema)


def test_the_mcp_door_still_validates():
    """It already did; this pins that the two doors now agree."""
    app = Veloce(title="Body", version="1.0.0", openapi_url=None)

    @app.post("/marked", expose_as_mcp_tool=True, mcp_description="Marked")
    async def marked(payload: Payload = Body()) -> dict:
        return {"note": payload.note}

    app.mount_mcp(transport="http", path="/mcp")
    client = TestClient(app)
    client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "p", "version": "1"},
            },
        },
        headers={"Accept": "application/json"},
    )
    result = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "marked", "arguments": {"payload": {"note": "hi"}}},
        },
        headers={"Accept": "application/json"},
    ).json()["result"]
    assert result["content"][0]["text"] == '{"note":"hi"}'


# ── the model is recognised at registration, not per request ─────────


def test_the_plan_records_the_model():
    """A per-request `is_pydantic_model` call would be introspection on the hot path."""
    from veloce._handler_plan import MK_BODY, build_plan

    async def handler(payload: Payload = Body()) -> dict:
        return {}

    slot = build_plan(handler).slots[0]
    assert slot.marker_kind == MK_BODY
    assert slot.model is Payload


def test_a_scalar_marker_records_no_model():
    from veloce._handler_plan import build_plan

    async def handler(note: str = Body()) -> dict:
        return {}

    assert build_plan(handler).slots[0].model is None
