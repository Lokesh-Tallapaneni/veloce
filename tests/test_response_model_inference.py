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


# ── Response-contract audit ──────────────────────────────────────────


def test_audit_flags_a_declared_model_that_contradicts_the_annotation():
    app = Veloce(openapi_url=None)

    @app.get("/x", response_model=Item)
    async def x() -> Other:
        return Other(other="v")

    findings = app.response_contract_audit()
    assert any("disagree" in f and "/x" in f for f in findings)


def test_audit_lists_routes_with_no_response_schema():
    app = Veloce(openapi_url=None)

    @app.get("/free")
    async def free():
        return {"a": 1}

    findings = app.response_contract_audit()
    assert any("publish no response schema" in f and "/free" in f for f in findings)


def test_audit_is_quiet_when_every_route_is_documented():
    app = Veloce(openapi_url=None)

    @app.get("/ok")
    async def ok() -> Item:
        return Item(id=1, name="a")

    assert app.response_contract_audit() == []
