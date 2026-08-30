"""The return annotation supplies the response model, and the contract audit."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from veloce import HTMLResponse, Veloce


class Item(BaseModel):
    id: int
    name: str


class ItemWithSecret(Item):
    secret: str


class Other(BaseModel):
    other: str


def _schema_for(app: Veloce, path: str, method: str = "get") -> dict | None:
    spec = app.test_client().get("/openapi.json").json()
    return spec["paths"][path][method]["responses"]["200"].get("content")


# ── Inference ────────────────────────────────────────────────────────


def test_return_annotation_supplies_the_openapi_schema():
    app = Veloce()

    @app.get("/items")
    async def items() -> Item:
        return Item(id=1, name="a")

    content = _schema_for(app, "/items")
    assert content is not None
    assert content["application/json"]["schema"]["$ref"].endswith("/Item")


def test_return_annotation_filters_extra_fields():
    # The annotation is enforced, not advisory: a richer subclass is filtered
    # down to the declared shape rather than leaking the extra field.
    app = Veloce()

    @app.get("/user")
    async def user() -> Item:
        return ItemWithSecret(id=1, name="ada", secret="SECRET")

    body = app.test_client().get("/user").json()
    assert body == {"id": 1, "name": "ada"}


def test_explicit_response_model_filters_a_subclass_too():
    # A subclass instance satisfies `isinstance` against the declared model, so
    # it must still be re-shaped rather than dumped as itself - otherwise a
    # base-model contract leaks whatever the subclass adds.
    app = Veloce()

    @app.get("/sub", response_model=Item)
    async def sub():
        return ItemWithSecret(id=1, name="ada", secret="SECRET")

    assert app.test_client().get("/sub").json() == {"id": 1, "name": "ada"}


def test_list_response_model_filters_subclass_elements():
    app = Veloce()

    @app.get("/subs", response_model=list[Item])
    async def subs():
        return [ItemWithSecret(id=1, name="ada", secret="SECRET")]

    assert app.test_client().get("/subs").json() == [{"id": 1, "name": "ada"}]


def test_explicit_response_model_wins_over_the_annotation():
    app = Veloce()

    @app.get("/x", response_model=Other)
    async def x() -> Item:
        return Other(other="v")

    body = app.test_client().get("/x").json()
    assert body == {"other": "v"}
    assert _schema_for(app, "/x")["application/json"]["schema"]["$ref"].endswith("/Other")


def test_explicit_none_opts_out_of_inference():
    # Keeping the annotation for a type checker while declaring no contract.
    app = Veloce()

    @app.get("/raw", response_model=None)
    async def raw() -> Item:
        return ItemWithSecret(id=1, name="ada", secret="SECRET")

    assert app.test_client().get("/raw").json() == {"id": 1, "name": "ada", "secret": "SECRET"}
    assert _schema_for(app, "/raw") is None


def test_transport_and_untyped_annotations_declare_no_contract():
    # An annotation naming no model must skip inference quietly - no manual
    # opt-out required for the shapes that cannot express a response schema.
    app = Veloce()

    @app.get("/any")
    async def any_ret() -> Any:
        return {"a": 1}

    @app.get("/html")
    async def html() -> HTMLResponse:
        return HTMLResponse("<p>hi</p>")

    @app.get("/dict")
    async def dict_ret() -> dict[str, Any]:
        return {"a": 1}

    @app.get("/bare")
    async def bare():
        return {"a": 1}

    client = app.test_client()
    for path in ("/any", "/dict", "/bare"):
        assert client.get(path).json() == {"a": 1}
        assert _schema_for(app, path) is None
    assert client.get("/html").status_code == 200
    assert _schema_for(app, "/html") is None


def test_list_annotation_documents_and_filters():
    app = Veloce()

    @app.get("/items")
    async def items() -> list[Item]:
        return [ItemWithSecret(id=1, name="ada", secret="SECRET")]

    schema = _schema_for(app, "/items")["application/json"]["schema"]
    assert schema["type"] == "array"
    assert schema["items"]["$ref"].endswith("/Item")
    # A `list[Model]` contract filters its elements too.
    assert app.test_client().get("/items").json() == [{"id": 1, "name": "ada"}]


def test_union_annotation_documents_alternatives():
    app = Veloce()

    @app.get("/either")
    async def either() -> Item | Other:
        return Item(id=1, name="a")

    schema = _schema_for(app, "/either")["application/json"]["schema"]
    refs = {v["$ref"].rsplit("/", 1)[-1] for v in schema["oneOf"]}
    assert refs == {"Item", "Other"}
    # A union documents its alternatives but does not filter - which member to
    # re-shape through is ambiguous - so the value serializes unchanged.
    assert app.test_client().get("/either").json() == {"id": 1, "name": "a"}


def test_optional_annotation_documents_null_and_allows_none():
    app = Veloce()

    @app.get("/maybe")
    async def maybe() -> Item | None:
        return None

    schema = _schema_for(app, "/maybe")["application/json"]["schema"]
    assert {"type": "null"} in schema["oneOf"]
    # Returning None under an optional contract must not fail the request.
    resp = app.test_client().get("/maybe")
    assert resp.status_code == 200
    assert resp.json() is None


# ── the contract findings reach a developer at startup ───────────────


def test_debug_logs_the_contract_findings_at_startup(caplog):
    # The findings must reach a developer at first boot, not only when someone
    # runs `veloce check` or reads the rendered docs.
    app = Veloce(openapi_url=None, debug=True)

    @app.get("/free")
    async def free():
        return {"a": 1}

    with caplog.at_level("WARNING"):
        app.test_client()  # constructing the client runs startup
    assert any("publish no response schema" in r.getMessage() for r in caplog.records)
