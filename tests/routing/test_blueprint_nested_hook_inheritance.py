"""A parent blueprint's hooks reach every route beneath it.

The guard case is the reason this file exists: `@parent.before_request` is where
an authentication check goes, and a route on a nested child is still a route
under the parent. It ran only when the child happened to declare a hook of the
same category, so an app was guarded or open depending on whether an unrelated
child had its own `before_request` - and nothing reported the difference.

The complement matters just as much and is tested here too: a child's hooks must
NOT reach a sibling. Scoping is what these buckets are for.
"""

from __future__ import annotations

import pytest

from veloce import Blueprint, HTTPException, Veloce
from veloce.testclient import TestClient


def _record(bp: Blueprint, log: list[str], label: str, category: str = "before") -> None:
    """Attach a hook of `category` to `bp` that appends `label` when it runs."""
    if category == "before":

        @bp.before_request
        async def _before(request):
            log.append(label)

    elif category == "after":

        @bp.after_request
        async def _after(request, response):
            log.append(label)
            return response

    else:

        @bp.teardown_request
        async def _teardown(exc):
            log.append(label)


# ── the guard case ───────────────────────────────────────────────────


@pytest.mark.parametrize("category", ["before", "after", "teardown"])
@pytest.mark.parametrize("child_declares_its_own", [False, True])
def test_a_parent_hook_runs_on_a_nested_route(category, child_declares_its_own):
    """The defect: this depended on whether the child declared one too."""
    log: list[str] = []
    app = Veloce(openapi_url=None)
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/c")
    _record(parent, log, "parent", category)
    if child_declares_its_own:
        _record(child, log, "child", category)

    @child.get("/x")
    async def x():
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent)

    assert TestClient(app).get("/p/c/x").status_code == 200
    assert "parent" in log


def test_a_parent_guard_rejects_a_nested_route():
    """The security shape, end to end: the guard must actually deny."""
    app = Veloce(openapi_url=None)
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/c")

    @parent.before_request
    async def guard(request):
        raise HTTPException(status_code=401, detail="denied")

    @child.get("/secret")
    async def secret():
        return {"secret": "leaked"}

    parent.register_blueprint(child)
    app.register_blueprint(parent)

    response = TestClient(app).get("/p/c/secret")
    assert response.status_code == 401
    assert "leaked" not in response.text


def test_the_hooks_run_outermost_first():
    log: list[str] = []
    app = Veloce(openapi_url=None)
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/c")
    _record(parent, log, "parent")
    _record(child, log, "child")

    @child.get("/x")
    async def x():
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent)
    TestClient(app).get("/p/c/x")
    assert log == ["parent", "child"]


# ── depth ────────────────────────────────────────────────────────────


def test_a_hook_reaches_a_grandchild_that_declares_nothing():
    """Two empty levels: the path must still exist for the chain to land on."""
    log: list[str] = []
    app = Veloce(openapi_url=None)
    top = Blueprint("top", url_prefix="/t")
    mid = Blueprint("mid", url_prefix="/m")
    leaf = Blueprint("leaf", url_prefix="/l")
    _record(top, log, "top")

    @leaf.get("/x")
    async def x():
        return {}

    mid.register_blueprint(leaf)
    top.register_blueprint(mid)
    app.register_blueprint(top)

    assert TestClient(app).get("/t/m/l/x").status_code == 200
    assert log == ["top"]


def test_every_level_of_the_chain_runs_in_order():
    log: list[str] = []
    app = Veloce(openapi_url=None)
    top = Blueprint("top", url_prefix="/t")
    mid = Blueprint("mid", url_prefix="/m")
    leaf = Blueprint("leaf", url_prefix="/l")
    for bp, label in ((top, "top"), (mid, "mid"), (leaf, "leaf")):
        _record(bp, log, label)

    @leaf.get("/x")
    async def x():
        return {}

    mid.register_blueprint(leaf)
    top.register_blueprint(mid)
    app.register_blueprint(top)
    TestClient(app).get("/t/m/l/x")
    assert log == ["top", "mid", "leaf"]


def test_a_middle_level_declaring_nothing_is_skipped_not_blocking():
    """top -> mid(nothing) -> leaf: top and leaf run, mid contributes nothing."""
    log: list[str] = []
    app = Veloce(openapi_url=None)
    top = Blueprint("top", url_prefix="/t")
    mid = Blueprint("mid", url_prefix="/m")
    leaf = Blueprint("leaf", url_prefix="/l")
    _record(top, log, "top")
    _record(leaf, log, "leaf")

    @leaf.get("/x")
    async def x():
        return {}

    mid.register_blueprint(leaf)
    top.register_blueprint(mid)
    app.register_blueprint(top)
    TestClient(app).get("/t/m/l/x")
    assert log == ["top", "leaf"]


# ── scoping: the property the buckets exist to provide ───────────────


def test_a_child_hook_does_not_reach_a_sibling():
    log: list[str] = []
    app = Veloce(openapi_url=None)
    parent = Blueprint("parent", url_prefix="/p")
    guarded = Blueprint("guarded", url_prefix="/g")
    open_bp = Blueprint("open", url_prefix="/o")
    _record(guarded, log, "guarded_only")

    @guarded.get("/x")
    async def gx():
        return {}

    @open_bp.get("/x")
    async def ox():
        return {}

    parent.register_blueprint(guarded)
    parent.register_blueprint(open_bp)
    app.register_blueprint(parent)

    client = TestClient(app)
    client.get("/p/o/x")
    assert log == []
    client.get("/p/g/x")
    assert log == ["guarded_only"]


def test_a_child_hook_does_not_reach_the_parent_own_routes():
    log: list[str] = []
    app = Veloce(openapi_url=None)
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/c")
    _record(child, log, "child")

    @parent.get("/direct")
    async def direct():
        return {}

    @child.get("/x")
    async def x():
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent)

    TestClient(app).get("/p/direct")
    assert log == []


def test_a_parent_hook_does_not_reach_an_unrelated_blueprint():
    log: list[str] = []
    app = Veloce(openapi_url=None)
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/c")
    other = Blueprint("other", url_prefix="/other")
    _record(parent, log, "parent")

    @child.get("/x")
    async def x():
        return {}

    @other.get("/x")
    async def ox():
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent)
    app.register_blueprint(other)

    TestClient(app).get("/other/x")
    assert log == []


def test_an_app_level_route_is_untouched_by_blueprint_hooks():
    log: list[str] = []
    app = Veloce(openapi_url=None)
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/c")
    _record(parent, log, "parent")

    @app.get("/plain")
    async def plain():
        return {}

    @child.get("/x")
    async def x():
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent)

    TestClient(app).get("/plain")
    assert log == []


# ── nothing declared anywhere ────────────────────────────────────────


def test_a_chain_with_no_hooks_registers_no_bucket():
    """The empty case must not start costing a per-request lookup."""
    app = Veloce(openapi_url=None)
    parent = Blueprint("parent", url_prefix="/p")
    child = Blueprint("child", url_prefix="/c")

    @child.get("/x")
    async def x():
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent)

    assert app._bp_before_hooks == {}
    assert app._bp_after_hooks == {}
    assert app._bp_teardown_hooks == {}
    assert TestClient(app).get("/p/c/x").status_code == 200
