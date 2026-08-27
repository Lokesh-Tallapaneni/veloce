"""Dataclass and `TypedDict` parameters.

Both describe an object shape without being a `BaseModel`, so they used to fall
to the scalar path: the schema advertised a string while the handler was handed
the raw mapping. A dataclass parameter then failed on every call, and a
`TypedDict` parameter accepted exactly what its own schema forbade. They are now
validated through a memoised Pydantic adapter, so the declared contract and the
runtime agree and match what a `BaseModel` parameter already did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

import pytest
from pydantic import BaseModel
from typing_extensions import TypedDict

from veloce import Veloce, _model_backend
from veloce._model_backend import ModelBackend, adapter_for, backend_of
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


@dataclass
class Point:
    x: int
    y: int = 0


@dataclass
class Nested:
    label: str
    point: Point


@dataclass
class WithFactory:
    tags: list[str] = field(default_factory=list)


class Movie(TypedDict):
    title: str
    year: int


class PartialMovie(TypedDict, total=False):
    title: str


class Plain(BaseModel):
    a: int


# ── Backend classification ───────────────────────────────────────────


@pytest.mark.parametrize("tp", [Point, Nested, WithFactory, Movie, PartialMovie])
def test_an_object_shaped_stdlib_type_is_adapted(tp):
    assert backend_of(tp) is ModelBackend.ADAPTED


@pytest.mark.parametrize("tp", [int, str, dict, list, Plain, object])
def test_other_annotations_keep_their_backend(tp):
    assert backend_of(tp) is not ModelBackend.ADAPTED


def test_a_dataclass_instance_is_not_a_model_annotation():
    """`dataclasses.is_dataclass` answers True for an instance; a slot holds a type."""
    assert backend_of(Point(x=1)) is not ModelBackend.ADAPTED


def test_the_adapter_is_built_once_per_type():
    """Adapter construction runs a schema build, so it must never repeat per call."""
    assert adapter_for(Point) is adapter_for(Point)


def test_an_unhashable_annotation_still_gets_a_working_adapter():
    """`Annotated[..., <unhashable marker>]` cannot key the `WeakKeyDictionary`,
    so `.get()` raises. The lookup falls through to an uncached adapter rather
    than propagating - the same weak-key/unhashable contract
    `_internal._is_async_callable` keeps over its own cache."""
    annotation = Annotated[int, []]
    with pytest.raises(TypeError):
        hash(annotation)
    assert adapter_for(annotation).validate_python(3) == 3


def test_an_unhashable_annotation_is_rebuilt_rather_than_cached():
    """It cannot be stored, so each call builds afresh; the contract is only
    that the result is correct, not that it is the same object."""
    annotation = Annotated[int, []]
    first, second = adapter_for(annotation), adapter_for(annotation)
    assert first is not second
    assert first.validate_python(7) == second.validate_python(7) == 7


def test_the_adapter_cache_survives_a_type_defined_in_a_function():
    """Weak keys let a locally defined type be collected with its scope; while
    it is alive the adapter must still be reused."""

    @dataclass
    class Local:
        v: int

    assert adapter_for(Local) is adapter_for(Local)
    assert adapter_for(Local).validate_python({"v": 2}) == Local(v=2)


# ── The MCP tool surface ─────────────────────────────────────────────


def _server(*tools) -> MCPServer:
    app = Veloce(title="Adapted", version="1.0.0", openapi_url=None)
    for fn in tools:
        app.mcp_tool(description=f"tool {fn.__name__}")(fn)
    return MCPServer(app)


async def _call(server: MCPServer, name: str, arguments: dict) -> dict:
    return await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        MCPSession(),
    )


async def _schema(server: MCPServer, name: str) -> dict:
    listing = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, MCPSession()
    )
    return next(t for t in listing["result"]["tools"] if t["name"] == name)["inputSchema"]


async def _text(server: MCPServer, name: str, arguments: dict) -> str:
    response = await _call(server, name, arguments)
    result = response["result"]
    assert not result.get("isError"), result["content"][0]["text"]
    return result["content"][0]["text"]


async def take_point(p: Point) -> dict:
    return {"x": p.x, "y": p.y, "is_point": isinstance(p, Point)}


async def take_movie(m: Movie) -> dict:
    return {"title": m["title"], "year": m["year"]}


async def test_a_dataclass_argument_reaches_the_handler_as_the_dataclass():
    """The bug: the raw dict arrived, so the first attribute access failed."""
    body = await _text(_server(take_point), "take_point", {"p": {"x": 1, "y": 2}})
    assert '"is_point":true' in body
    assert '"x":1' in body and '"y":2' in body


async def test_a_typeddict_argument_reaches_the_handler_as_a_mapping():
    body = await _text(_server(take_movie), "take_movie", {"m": {"title": "Up", "year": 2009}})
    assert '"title":"Up"' in body


async def test_the_declared_schema_is_an_object_not_a_string():
    """Schema and runtime disagreed: the schema said string, the runtime required a mapping."""
    schema = await _schema(_server(take_point), "take_point")
    assert schema["properties"]["p"] == {"$ref": "#/$defs/Point"}


async def test_the_schema_publishes_the_fields_and_which_are_required():
    schema = await _schema(_server(take_point), "take_point")
    point = schema["$defs"]["Point"]
    assert sorted(point["properties"]) == ["x", "y"]
    assert point["required"] == ["x"]
    assert point["properties"]["y"]["default"] == 0


async def test_a_typeddict_total_false_marks_nothing_required():
    async def take_partial(m: PartialMovie) -> dict:
        return dict(m)

    schema = await _schema(_server(take_partial), "take_partial")
    assert "required" not in schema["$defs"]["PartialMovie"]


async def test_a_nested_dataclass_resolves_through_defs():
    async def take_nested(n: Nested) -> dict:
        return {"label": n.label, "x": n.point.x}

    server = _server(take_nested)
    schema = await _schema(server, "take_nested")
    assert "Point" in schema["$defs"], schema["$defs"].keys()
    body = await _text(server, "take_nested", {"n": {"label": "a", "point": {"x": 7}}})
    assert '"x":7' in body


async def test_a_default_factory_field_is_optional():
    async def take_factory(w: WithFactory) -> dict:
        return {"tags": w.tags}

    body = await _text(_server(take_factory), "take_factory", {"w": {}})
    assert '"tags":[]' in body


# ── Validation and coercion ──────────────────────────────────────────


async def test_a_value_is_coerced_onto_the_declared_field_type():
    body = await _text(_server(take_point), "take_point", {"p": {"x": "5"}})
    assert '"x":5' in body


async def test_a_missing_required_field_is_reported_not_crashed():
    response = await _call(_server(take_point), "take_point", {"p": {"y": 1}})
    result = response["result"]
    assert result["isError"] is True
    assert "x" in result["content"][0]["text"]


async def test_a_wrong_typed_field_is_rejected():
    response = await _call(_server(take_point), "take_point", {"p": {"x": "not-a-number"}})
    assert response["result"]["isError"] is True


async def test_a_non_object_argument_is_rejected():
    """The runtime required a mapping while the schema advertised a string."""
    response = await _call(_server(take_point), "take_point", {"p": "5"})
    assert response["result"]["isError"] is True


# ── The HTTP path uses the same classification ───────────────────────


def test_a_dataclass_body_is_validated_over_http():
    app = Veloce(title="AdaptedHTTP", openapi_url=None)

    @app.post("/point")
    async def create(p: Point) -> dict:
        return {"x": p.x, "y": p.y, "is_point": isinstance(p, Point)}

    with app.test_client() as client:
        ok = client.post("/point", json={"x": 3, "y": 4})
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"x": 3, "y": 4, "is_point": True}

        bad = client.post("/point", json={"y": 4})
        assert bad.status_code == 422, bad.text


def test_a_typeddict_body_is_validated_over_http():
    app = Veloce(title="AdaptedHTTP2", openapi_url=None)

    @app.post("/movie")
    async def create(m: Movie) -> dict:
        return {"title": m["title"]}

    with app.test_client() as client:
        assert client.post("/movie", json={"title": "Up", "year": 2009}).json() == {"title": "Up"}
        assert client.post("/movie", json={"title": "Up"}).status_code == 422


def test_an_empty_body_for_a_required_dataclass_is_a_validation_error():
    app = Veloce(title="AdaptedHTTP3", openapi_url=None)

    @app.post("/point")
    async def create(p: Point) -> dict:
        return {"x": p.x}

    with app.test_client() as client:
        assert client.post("/point", content=b"").status_code == 422


def test_an_optional_dataclass_body_may_be_omitted():
    app = Veloce(title="AdaptedHTTP4", openapi_url=None)

    @app.post("/point")
    async def create(p: Point | None = None) -> dict:
        return {"got": p is not None}

    with app.test_client() as client:
        assert client.post("/point", content=b"").json() == {"got": False}


# ── A BaseModel parameter is unaffected ──────────────────────────────


async def test_a_pydantic_parameter_still_behaves_identically():
    async def take_plain(m: Plain) -> dict:
        return {"a": m.a}

    server = _server(take_plain)
    schema = await _schema(server, "take_plain")
    assert schema["properties"]["m"] == {"$ref": "#/$defs/Plain"}
    assert '"a":2' in await _text(server, "take_plain", {"m": {"a": 2}})


def test_openapi_documents_a_dataclass_body():
    app = Veloce(title="AdaptedDocs", openapi_url="/openapi.json")

    @app.post("/point")
    async def create(p: Point) -> dict:
        return {"x": p.x}

    with app.test_client() as client:
        schema = client.get("/openapi.json").json()
    assert "Point" in schema["components"]["schemas"]
    assert sorted(schema["components"]["schemas"]["Point"]["properties"]) == ["x", "y"]


def test_the_adapter_cache_holds_its_keys_weakly():
    """Weak keys keep the framework from pinning every type it ever adapted.

    Pydantic keeps its own internal caches, so this asserts the structure Veloce
    controls rather than that a type actually becomes collectable.
    """
    import weakref

    assert isinstance(_model_backend._adapters, weakref.WeakKeyDictionary)
