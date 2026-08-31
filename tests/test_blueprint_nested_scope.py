"""A nested blueprint's registrations reach its own routes, not its siblings'.

`Blueprint.register_blueprint` scoped a child's error handlers under the child's
dotted name - with a comment saying a child handler must never catch a sibling's
routes - and then merged five other categories into the parent's own lists with
a plain `extend`. Those five became the parent's, so a child's `before_request`
guard ran on a sibling child's routes, and a child's `url_value_preprocessor`
rewrote a sibling's path params.

The app already bucketed hooks by blueprint name and read them per request; the
nested path simply never produced sub-names to feed it. Both halves now go
through one table of scoped categories, and the ancestor chain is flattened at
registration so the per-request path is still a single lookup.
"""

from __future__ import annotations

import pytest

from veloce import Blueprint, BuildError, JSONResponse, Veloce
from veloce.testclient import TestClient

_ORDER: list[str] = []


@pytest.fixture(autouse=True)
def _reset_order():
    _ORDER.clear()


def _leaf(bp: Blueprint, path: str, name: str):
    @bp.get(path, name=name)
    async def view():
        return {"ok": name}

    return view


# ── A child's registration must not reach a sibling ──────────────────


def _two_children(register) -> TestClient:
    """One parent, two children; `register` arms only the first."""
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/child")
    sibling = Blueprint("sib", url_prefix="/sib")

    _leaf(child, "/c", "c")
    _leaf(sibling, "/s", "s")
    register(child)

    parent.register_blueprint(child)
    parent.register_blueprint(sibling)
    app = Veloce(openapi_url=None)
    app.register_blueprint(parent)
    return TestClient(app)


def test_a_childs_before_request_does_not_run_on_a_sibling():
    """The reported defect: a guard on one child blocked a sibling's route."""

    def arm(bp: Blueprint) -> None:
        @bp.before_request
        async def guard(request):
            return JSONResponse({"blocked": "by child"}, status_code=403)

    client = _two_children(arm)
    assert client.get("/p/sib/s").status_code == 200
    assert client.get("/p/child/c").status_code == 403


def test_a_childs_after_request_does_not_run_on_a_sibling():
    def arm(bp: Blueprint) -> None:
        @bp.after_request
        async def stamp(request, response):
            response.headers["X-Child"] = "yes"
            return response

    client = _two_children(arm)
    assert "x-child" not in client.get("/p/sib/s").headers
    assert client.get("/p/child/c").headers["x-child"] == "yes"


def test_a_childs_teardown_does_not_run_on_a_sibling():
    def arm(bp: Blueprint) -> None:
        @bp.teardown_request
        async def note(exc):
            _ORDER.append("child-teardown")

    client = _two_children(arm)
    client.get("/p/sib/s")
    assert _ORDER == []
    client.get("/p/child/c")
    assert _ORDER == ["child-teardown"]


def test_a_childs_url_value_preprocessor_does_not_run_on_a_sibling():
    """The second reported defect: it rewrote a sibling's path params."""
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/child")
    sibling = Blueprint("sib", url_prefix="/sib")

    @child.url_value_preprocessor
    def strip(endpoint, values):
        if values:
            values.pop("lang", None)

    @child.get("/{lang}/c")
    async def cview(lang: str):
        return {"lang": lang}

    @sibling.get("/{lang}/s")
    async def sview(lang: str):
        return {"lang": lang}

    parent.register_blueprint(child)
    parent.register_blueprint(sibling)
    app = Veloce(openapi_url=None)
    app.register_blueprint(parent)
    client = TestClient(app)

    assert client.get("/p/sib/en/s").json() == {"lang": "en"}
    # The child's own route still loses the value its preprocessor popped.
    assert client.get("/p/child/en/c").status_code == 422


def test_a_childs_url_defaults_does_not_run_on_a_sibling():
    """`url_defaults` fires while building a URL, so drive it through url_for."""
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/child")
    sibling = Blueprint("sib", url_prefix="/sib")

    @child.url_defaults
    def supply(endpoint, values):
        values.setdefault("lang", "fr")

    @child.get("/{lang}/c", name="c")
    async def cview(lang: str):
        return {"lang": lang}

    @sibling.get("/{lang}/s", name="s")
    async def sview(lang: str):
        return {"lang": lang}

    parent.register_blueprint(child)
    parent.register_blueprint(sibling)
    app = Veloce(openapi_url=None)
    app.register_blueprint(parent)

    # The child's default fills in its own route's missing parameter...
    assert app.url_for("parent.child.c") == "/p/child/fr/c"
    # ...and does not reach the sibling's, which still requires the value.
    with pytest.raises(BuildError, match="parent.sib.s"):
        app.url_for("parent.sib.s")


# ── The ancestor chain still applies, in order ───────────────────────


def _ordered_app() -> TestClient:
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/child")
    grand = Blueprint("grand", url_prefix="/grand")

    for bp in (parent, child, grand):

        @bp.before_request
        async def before(request, _n=bp.name):
            _ORDER.append(f"before:{_n}")

        @bp.after_request
        async def after(request, response, _n=bp.name):
            _ORDER.append(f"after:{_n}")
            return response

    _leaf(grand, "/g", "g")
    child.register_blueprint(grand)
    parent.register_blueprint(child)
    app = Veloce(openapi_url=None)
    app.register_blueprint(parent)
    return TestClient(app)


def test_every_ancestors_hook_still_runs_on_a_descendant():
    client = _ordered_app()
    assert client.get("/p/child/grand/g").status_code == 200
    assert [e for e in _ORDER if e.startswith("before:")] == [
        "before:parent",
        "before:child",
        "before:grand",
    ]


def test_after_hooks_unwind_innermost_first():
    client = _ordered_app()
    client.get("/p/child/grand/g")
    assert [e for e in _ORDER if e.startswith("after:")] == [
        "after:grand",
        "after:child",
        "after:parent",
    ]


# ── What must not change ─────────────────────────────────────────────


def test_a_flat_blueprints_hook_still_runs_on_its_routes():
    """The common case has no nesting and must be untouched."""
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.before_request
    async def guard(request):
        _ORDER.append("ran")

    _leaf(bp, "/x", "x")
    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)
    client = TestClient(app)

    assert client.get("/bp/x").status_code == 200
    assert _ORDER == ["ran"]


def test_two_sibling_blueprints_on_the_app_stay_independent():
    """The un-nested form of the same question, which already worked."""
    a = Blueprint("a", url_prefix="/a")
    b = Blueprint("b", url_prefix="/b")

    @a.before_request
    async def guard(request):
        return JSONResponse({"blocked": True}, status_code=403)

    _leaf(a, "/x", "ax")
    _leaf(b, "/y", "by")
    app = Veloce(openapi_url=None)
    app.register_blueprint(a)
    app.register_blueprint(b)
    client = TestClient(app)

    assert client.get("/b/y").status_code == 200
    assert client.get("/a/x").status_code == 403


def test_a_childs_error_handler_is_still_scoped():
    """Error handlers were already correct; the fix must not regress them."""
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/child")
    sibling = Blueprint("sib", url_prefix="/sib")

    @child.errorhandler(ValueError)
    async def caught(request, exc):
        return JSONResponse({"caught": "by child"}, status_code=200)

    @child.get("/c")
    async def cview():
        raise ValueError("boom")

    @sibling.get("/s")
    async def sview():
        raise ValueError("boom")

    parent.register_blueprint(child)
    parent.register_blueprint(sibling)
    app = Veloce(openapi_url=None)
    app.register_blueprint(parent)
    client = TestClient(app)

    assert client.get("/p/child/c").json() == {"caught": "by child"}
    # The sibling's identical raise is not caught by the child's handler; it
    # falls through to the framework's own 500.
    assert client.get("/p/sib/s").status_code == 500


def test_an_app_level_route_is_unaffected_by_any_blueprint():
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.before_request
    async def guard(request):
        return JSONResponse({"blocked": True}, status_code=403)

    _leaf(bp, "/x", "x")
    app = Veloce(openapi_url=None)

    @app.get("/top")
    async def top():
        return {"ok": True}

    app.register_blueprint(bp)
    client = TestClient(app)
    assert client.get("/top").status_code == 200
