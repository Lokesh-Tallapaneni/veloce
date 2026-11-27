"""OpenAPI emission of Query/Path examples (JSON Schema 2020-12)."""

from __future__ import annotations

from tests._openapi import parameter
from veloce import Query, Veloce
from veloce.contrib.openapi import get_openapi_schema


def _param_schema(app: Veloce, path: str, name: str) -> dict:
    found = parameter(get_openapi_schema(app), path, name)
    assert found is not None, f"parameter {name!r} not found"
    return found["schema"]


def test_examples_emitted_to_param_schema():
    app = Veloce()

    @app.get("/search")
    async def search(q: str = Query(default="", examples=["cats", "dogs"])):
        return {}

    sch = _param_schema(app, "/search", "q")
    assert sch["examples"] == ["cats", "dogs"]


def test_no_examples_means_no_keyword():
    app = Veloce()

    @app.get("/plain")
    async def plain(x: str = Query(default="")):
        return {}

    sch = _param_schema(app, "/plain", "x")
    assert "examples" not in sch


def test_examples_single_value():
    app = Veloce()

    @app.get("/one")
    async def one(n: int = Query(default=1, examples=[42])):
        return {}

    sch = _param_schema(app, "/one", "n")
    assert sch["examples"] == [42]


def test_examples_coexist_with_constraints():
    app = Veloce()

    @app.get("/c")
    async def c(n: int = Query(default=1, ge=1, le=100, examples=[50])):
        return {}

    sch = _param_schema(app, "/c", "n")
    assert sch["examples"] == [50]
    assert sch["minimum"] == 1
    assert sch["maximum"] == 100
