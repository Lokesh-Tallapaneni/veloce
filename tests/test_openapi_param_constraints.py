"""OpenAPI emission of Query/Path constraint keywords."""

from __future__ import annotations

from tests._openapi import parameters
from veloce import Query, Veloce
from veloce.contrib.openapi import get_openapi_schema


def _params(app: Veloce, path: str, method: str = "get") -> list[dict]:
    # `get_openapi_schema`, not the served document: this module is about what
    # generation emits.
    return parameters(get_openapi_schema(app), path, method)


def _schema_for(params: list[dict], name: str) -> dict:
    for p in params:
        if p["name"] == name:
            return p["schema"]
    raise AssertionError(f"parameter {name!r} not found")


def test_ge_le_emitted_as_minimum_maximum():
    app = Veloce()

    @app.get("/a")
    async def a(n: int = Query(default=1, ge=1, le=100)):
        return {}

    sch = _schema_for(_params(app, "/a"), "n")
    assert sch["minimum"] == 1
    assert sch["maximum"] == 100


def test_gt_lt_emitted_as_exclusive_bounds():
    app = Veloce()

    @app.get("/b")
    async def b(n: int = Query(default=5, gt=0, lt=10)):
        return {}

    sch = _schema_for(_params(app, "/b"), "n")
    assert sch["exclusiveMinimum"] == 0
    assert sch["exclusiveMaximum"] == 10


def test_min_max_length_emitted():
    app = Veloce()

    @app.get("/c")
    async def c(s: str = Query(default="", min_length=2, max_length=8)):
        return {}

    sch = _schema_for(_params(app, "/c"), "s")
    assert sch["minLength"] == 2
    assert sch["maxLength"] == 8


def test_pattern_emitted():
    app = Veloce()

    @app.get("/d")
    async def d(code: str = Query(default="", pattern=r"^[A-Z]{3}$")):
        return {}

    sch = _schema_for(_params(app, "/d"), "code")
    assert sch["pattern"] == r"^[A-Z]{3}$"


def test_no_constraints_means_no_keywords():
    app = Veloce()

    @app.get("/e")
    async def e(plain: str = Query(default="")):
        return {}

    sch = _schema_for(_params(app, "/e"), "plain")
    for kw in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        assert kw not in sch
