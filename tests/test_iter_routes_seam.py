"""`app.iter_routes()` - the public read side of the route table.



`app.routes` projects `RouteInfo`'s 48 fields down to six. That is enough to

print a route table and not enough for anything that inspects a route, so every

real consumer read the table through the private `_collect_all_routes()`

instead: ten source modules and sixteen test modules, 37 call sites. A private

name with 37 callers across the package and its tests is not private; it is an

undeclared public API, and an out-of-tree extension that wants the same thing -

an admin route browser, a linter, a client generator - has no supported way to

ask.



`iter_routes()` returns exactly what those callers wanted, the

`(method, path, info)` records themselves. `app.routes` is unchanged: it stays

the summary view, and these tests pin that it did not move.

"""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Depends, Router, Veloce
from veloce.routing import RouteInfo


class Item(BaseModel):
    name: str


def _app() -> Veloce:

    app = Veloce(openapi_url=None)

    async def dep() -> int:

        return 1

    @app.get("/items", response_model=Item, tags=["t"], summary="S")
    async def items(n: int = Depends(dep)) -> Item:

        return Item(name="x")

    @app.post("/hidden", include_in_schema=False)
    async def hidden():

        return {}

    @app.websocket("/ws")
    async def ws(websocket):

        await websocket.accept()

    return app


# ── it returns the records, not a projection ─────────────────────────


def test_iter_routes_yields_route_info_objects():
    """The defect: `app.routes` hands back dicts of six keys."""

    _, _, info = next(r for r in _app().iter_routes() if r[1] == "/items")

    assert isinstance(info, RouteInfo)


def test_the_records_carry_fields_app_routes_drops():
    """`response_model` is the field the consumers were reaching in for."""

    _, _, info = next(r for r in _app().iter_routes() if r[1] == "/items")

    assert info.response_model is Item


def test_a_record_carries_its_dependencies():

    _, _, info = next(r for r in _app().iter_routes() if r[1] == "/items")

    assert info.dependencies is not None or info.handler is not None


def test_the_tuple_shape_is_method_path_info():

    method, path, info = next(r for r in _app().iter_routes() if r[1] == "/items")

    assert method == "GET"

    assert path == "/items"

    assert info.name == "items"


# ── it agrees with the private collector it replaces ─────────────────


def test_iter_routes_matches_the_private_collector():
    """The seam is the same table, not a second one that can drift."""

    app = _app()

    assert app.iter_routes() == app._collect_all_routes()


def test_include_hidden_matches_the_private_collector():

    app = _app()

    assert app.iter_routes(include_hidden=True) == app._collect_all_routes(True)


# ── hidden routes are opt-in ─────────────────────────────────────────


def test_hidden_routes_are_omitted_by_default():

    paths = {path for _, path, _ in _app().iter_routes()}

    assert "/hidden" not in paths

    assert "/items" in paths


def test_hidden_routes_appear_when_asked_for():

    paths = {path for _, path, _ in _app().iter_routes(include_hidden=True)}

    assert "/hidden" in paths


def test_websocket_routes_are_hidden_by_default():

    paths = {path for _, path, _ in _app().iter_routes()}

    assert "/ws" not in paths


def test_websocket_routes_appear_when_asked_for():

    paths = {path for _, path, _ in _app().iter_routes(include_hidden=True)}

    assert "/ws" in paths


def test_include_hidden_is_keyword_only():
    """Positional would let `iter_routes(True)` read as a path argument."""

    import inspect

    kind = inspect.signature(Veloce.iter_routes).parameters["include_hidden"].kind

    assert kind is inspect.Parameter.KEYWORD_ONLY


# ── and the old summary view is untouched ────────────────────────────


def test_app_routes_still_returns_the_six_key_summary():

    entry = next(r for r in _app().routes if r["path"] == "/items")

    assert sorted(entry) == ["deprecated", "method", "name", "path", "summary", "tags"]


def test_app_routes_still_reports_its_values():

    entry = next(r for r in _app().routes if r["path"] == "/items")

    assert entry["summary"] == "S"

    assert entry["tags"] == ["t"]


def test_the_two_views_describe_the_same_routes():

    app = _app()

    assert {(r["method"], r["path"]) for r in app.routes} == {
        (m, p) for m, p, _ in app.iter_routes()
    }


# ── a router that is not an app exposes it too ───────────────────────


def test_iter_routes_is_available_on_a_plain_router():
    """It lives on `Router`, so a blueprint or sub-router answers the same way."""

    router = Router()

    @router.get("/r")
    async def r():

        return {}

    assert [(m, p) for m, p, _ in router.iter_routes()] == [("GET", "/r")]


# ── it tracks registration ───────────────────────────────────────────


def test_a_route_added_later_appears():
    """The negative: a cached snapshot would pass every test above."""

    app = _app()

    before = len(app.iter_routes())

    @app.get("/late")
    async def late():

        return {}

    assert len(app.iter_routes()) == before + 1

    assert "/late" in {p for _, p, _ in app.iter_routes()}


# ── and nothing outside the router package reads the private name ────


def test_no_subpackage_reads_the_route_table_privately():
    """The guardrail: an underscore name must not cross a subpackage boundary.



    `contrib/mcp`, `contrib/openapi`, `middleware/security` and `audit.py` all

    read the table through `_collect_all_routes` before this seam existed.



    `routing/` owns the method and `app/` is the same class - `Veloce` extends
    `Router`, so an app mixin reading it is an in-class access. A `Router`
    subclass calling `self._collect_all_routes()` is not a crossing either;
    `blueprints.py` inherits the method. Everything else is.
    """
    import re
    from pathlib import Path

    foreign = re.compile(r"(?<!self)[.]_collect_all_routes")
    root = Path(__file__).resolve().parents[1] / "src" / "veloce"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path.parts[len(root.parts)] not in {"routing", "app"}
        and foreign.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], offenders


def test_the_boundary_scan_can_actually_fail():
    """The regex above once ended in a stray control character and so matched
    nothing - it passed while four modules were offending. Point it at strings
    known to offend and known not to."""
    import re

    foreign = re.compile(r"(?<!self)[.]_collect_all_routes")
    assert foreign.search("for m, p, i in app._collect_all_routes():")
    assert foreign.search("self._app._collect_all_routes()")
    assert not foreign.search("self._collect_all_routes()")
