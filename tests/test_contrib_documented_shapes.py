"""Two `contrib` promises that the code did not keep.

**1. `Sequence[MyModel]` lost its response schema.**
`_response_model_to_schema`'s docstring lists "`list[MyModel]` (or any
`Sequence[MyModel]`) -> array-of-refs". Only `origin is list` was handled, so
every other sequence fell to the "anything else -> `None`" case and the operation
documented no response body at all:

    response_model=list[Item]      {"type":"array","items":{"$ref": ...}}
    response_model=Sequence[Item]  null
    response_model=tuple[Item,...] null

**2. The `CompletionResult` usage example raised.**
It declared `async def complete_name(value: str)`, but a completer is always
invoked as `completer(value, context)`. Following the documented example gave a
`TypeError` at call time, surfacing to the client as `-32603` internal error.

A third claim was investigated and **not** changed: the comment saying the `Any`
branch "lets `dict[str, Any]` emit `additionalProperties: {}`". The `dict` branch
fifty lines below deliberately omits `additionalProperties`, with its reason
stated — a typed value would advertise a parameter shape the resolver always
rejects. Only the earlier comment, which contradicted it, was corrected.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel

from veloce import Veloce
from veloce.contrib.mcp.completion import CompletionResult
from veloce.contrib.openapi import _python_type_to_schema
from veloce.testclient import TestClient

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


class Item(BaseModel):
    a: int


def _schema_for(response_model: Any) -> Any:
    app = Veloce(title="S", version="1.0.0")

    @app.get("/x", response_model=response_model)
    async def x():
        return [Item(a=1)]

    operation = app.openapi()["paths"]["/x"]["get"]
    content = operation["responses"]["200"].get("content", {})
    return content.get("application/json", {}).get("schema")


# ── every sequence of models documents its item type ─────────────────


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(list[Item], id="list"),
        pytest.param(Sequence[Item], id="Sequence"),
        pytest.param(tuple[Item, ...], id="tuple"),
        pytest.param(set[Item], id="set"),
        pytest.param(frozenset[Item], id="frozenset"),
    ],
)
def test_a_sequence_of_models_is_an_array_of_refs(model):
    """The defect: everything but `list` documented no response schema."""
    assert _schema_for(model) == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/Item"},
    }


@pytest.mark.parametrize("model", [list[int], Sequence[int], tuple[int, ...]])
def test_a_sequence_of_non_models_is_an_open_array(model):
    """The existing fallback for a sequence whose item is not a model."""
    assert _schema_for(model) == {"type": "array", "items": {}}


def test_a_bare_model_is_still_a_ref():
    assert _schema_for(Item) == {"$ref": "#/components/schemas/Item"}


def test_a_union_is_still_a_oneof():
    schema = _schema_for(Item | None)
    assert "oneOf" in schema


def test_an_undocumentable_shape_still_yields_no_schema():
    """The negative: widening must not start documenting everything."""
    assert _schema_for(int) is None


def test_a_sequence_response_still_serialises():
    """End to end: documenting it must not change what is sent."""
    app = Veloce(title="S", version="1.0.0", openapi_url=None)

    @app.get("/x", response_model=Sequence[Item])
    async def x():
        return [Item(a=1), Item(a=2)]

    assert TestClient(app).get("/x").json() == [{"a": 1}, {"a": 2}]


def test_the_inferred_list_case_is_unchanged():
    """Return-annotation inference still accepts `list[Model]` only."""
    app = Veloce(title="S", version="1.0.0")

    @app.get("/x")
    async def x() -> list[Item]:
        return [Item(a=1)]

    schema = app.openapi()["paths"]["/x"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert schema == {"type": "array", "items": {"$ref": "#/components/schemas/Item"}}


# ── a dict parameter still omits additionalProperties, on purpose ────


def test_a_dict_schema_stays_open():
    """Deliberate: a typed value would advertise a shape the resolver rejects."""

    assert _python_type_to_schema(dict[str, Any]) == {"type": "object"}
    assert _python_type_to_schema(dict[str, int]) == {"type": "object"}


def test_any_still_renders_as_an_empty_schema():

    assert _python_type_to_schema(Any) == {}


# ── the documented completer example runs ────────────────────────────


def _completion_app():
    app = Veloce(title="C", version="1.0.0", openapi_url=None)

    @app.mcp_prompt(description="Greet someone")
    async def greet(name: str) -> str:
        return f"hi {name}"

    # Exactly the signature the `CompletionResult` docstring now shows.
    @app.mcp_completer(prompt="greet", argument="name")
    async def complete_name(value: str, siblings: dict[str, str]) -> CompletionResult:
        matches = [n for n in ("ada", "alan", "grace") if n.startswith(value)]
        return CompletionResult(matches[:100], total=len(matches))

    app.mount_mcp(transport="http", path="/mcp")
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    return client


def _complete(client: TestClient, value: str) -> dict:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "completion/complete",
            "params": {
                "ref": {"type": "ref/prompt", "name": "greet"},
                "argument": {"name": "name", "value": value},
            },
        },
        headers={"Accept": "application/json"},
    ).json()


def test_the_documented_completer_signature_works():
    """The defect: the documented one-parameter form raised -32603."""
    result = _complete(_completion_app(), "a")["result"]
    assert result["completion"]["values"] == ["ada", "alan"]


def test_the_declared_total_is_carried():
    """What `CompletionResult` exists for."""
    assert _complete(_completion_app(), "a")["result"]["completion"]["total"] == 2


def test_a_value_matching_nothing_completes_empty():
    result = _complete(_completion_app(), "zzz")["result"]
    assert result["completion"]["values"] == []


def test_an_argument_with_no_completer_is_not_an_error():
    """The module's own promise: a client may always probe."""
    app = Veloce(title="C", version="1.0.0", openapi_url=None)

    @app.mcp_prompt(description="Greet someone")
    async def greet(name: str) -> str:
        return f"hi {name}"

    app.mount_mcp(transport="http", path="/mcp")
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    body = _complete(client, "a")
    assert "error" not in body
    assert body["result"]["completion"]["values"] == []


def test_a_one_parameter_completer_is_the_shape_that_fails():
    """Pinned so the docstring cannot drift back to it."""
    app = Veloce(title="C", version="1.0.0", openapi_url=None)

    @app.mcp_prompt(description="Greet someone")
    async def greet(name: str) -> str:
        return f"hi {name}"

    @app.mcp_completer(prompt="greet", argument="name")
    async def bad(value: str) -> CompletionResult:  # missing the sibling context
        return CompletionResult([value])

    app.mount_mcp(transport="http", path="/mcp")
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    assert "error" in _complete(client, "a")
