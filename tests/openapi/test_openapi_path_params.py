"""Every path template variable is documented as a path parameter.

A route's path parameters are part of its contract whether or not the handler
signature names one: a dependency reading `request.path_params` consumes the
same segment. Omitting it also makes the document invalid — OpenAPI 3.1 requires
every template expression in a path to have a corresponding required `path`
parameter — so a client reading the schema had no way to know to supply it.
"""

from __future__ import annotations

import pytest

from veloce import Depends, Path, Request, Veloce


def _params(app: Veloce, path: str, method: str = "get") -> list[dict]:
    return app.openapi()["paths"][path][method].get("parameters", [])


def _app() -> Veloce:
    return Veloce(title="Params", version="1.0.0")


def _read_item(request: Request) -> str:
    """Consumes a path parameter no handler parameter declares."""
    return str(request.path_params.get("item_id", "<absent>"))


# ── A parameter no signature declares ────────────────────────────────


def test_a_path_variable_only_a_dependency_reads_is_documented():
    app = _app()

    @app.get("/loc/{item_id}")
    async def loc(value: str = Depends(_read_item)) -> dict:
        return {"value": value}

    assert _params(app, "/loc/{item_id}") == [
        {"name": "item_id", "in": "path", "required": True, "schema": {"type": "string"}}
    ]


def test_such_a_parameter_is_required():
    """A route cannot match without its segment, so the parameter is never optional."""
    app = _app()

    @app.get("/loc/{item_id}")
    async def loc(value: str = Depends(_read_item)) -> dict:
        return {"value": value}

    assert _params(app, "/loc/{item_id}")[0]["required"] is True


@pytest.mark.parametrize(
    ("spec", "schema"),
    [
        ("", {"type": "string"}),
        (":int", {"type": "integer"}),
        (":float", {"type": "number"}),
        (":uuid", {"type": "string", "format": "uuid"}),
        (":path", {"type": "string", "format": "path"}),
        (":date", {"type": "string", "format": "date"}),
    ],
)
def test_the_converter_supplies_the_documented_type(spec: str, schema: dict):
    app = _app()

    @app.get(f"/loc/{{item_id{spec}}}")
    async def loc(value: str = Depends(_read_item)) -> dict:
        return {"value": value}

    assert _params(app, "/loc/{item_id}")[0]["schema"] == schema


def test_an_unrecognised_spec_is_documented_as_a_string():
    """A raw-regex or custom converter still matches text; that is what is said."""
    app = _app()

    @app.get("/code/{item_id:[0-9]{2}}")
    async def code(value: str = Depends(_read_item)) -> dict:
        return {"value": value}

    assert _params(app, "/code/{item_id}")[0]["schema"] == {"type": "string"}


def test_every_variable_of_a_multi_parameter_route_is_documented():
    app = _app()

    def read_both(request: Request) -> str:
        return f"{request.path_params['org']}/{request.path_params['repo']}"

    @app.get("/{org}/{repo}")
    async def repo(value: str = Depends(read_both)) -> dict:
        return {"value": value}

    assert sorted(p["name"] for p in _params(app, "/{org}/{repo}")) == ["org", "repo"]


# ── A declared parameter is unaffected ───────────────────────────────


def test_a_declared_path_parameter_is_documented_once():
    app = _app()

    @app.get("/item/{item_id}")
    async def item(item_id: int) -> dict:
        return {"item_id": item_id}

    params = _params(app, "/item/{item_id}")
    assert len(params) == 1
    assert params[0]["schema"]["type"] == "integer"


def test_a_declared_parameter_keeps_its_own_documentation():
    """The signature is the more precise source, so it is not overwritten."""

    app = _app()

    @app.get("/item/{item_id}")
    async def item(item_id: int = Path(description="The item's id", ge=1)) -> dict:
        return {"item_id": item_id}

    # Veloce carries both on the Schema Object, which OpenAPI 3.1 allows.
    param = _params(app, "/item/{item_id}")[0]
    assert param["schema"]["description"] == "The item's id"
    assert param["schema"]["minimum"] == 1


def test_a_route_with_no_variables_documents_no_path_parameters():
    app = _app()

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    assert _params(app, "/health") == []


def test_a_mix_of_declared_and_undeclared_variables_is_complete():
    app = _app()

    def read_rev(request: Request) -> str:
        return str(request.path_params.get("rev", "head"))

    @app.get("/repo/{name}/tree/{rev}")
    async def tree(name: str, rev_value: str = Depends(read_rev)) -> dict:
        return {"name": name, "rev": rev_value}

    by_name = {p["name"]: p for p in _params(app, "/repo/{name}/tree/{rev}")}
    assert set(by_name) == {"name", "rev"}
    assert all(p["required"] for p in by_name.values())


def test_the_query_parameters_of_the_same_route_are_untouched():
    app = _app()

    @app.get("/search/{scope}")
    async def search(q: str, limit: int = 10, value: str = Depends(_read_item)) -> dict:
        return {"q": q, "limit": limit}

    locations = {(p["name"], p["in"]) for p in _params(app, "/search/{scope}")}
    assert ("q", "query") in locations
    assert ("limit", "query") in locations
    assert ("scope", "path") in locations
