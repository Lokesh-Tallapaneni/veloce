"""A blueprint's URL processors cost only the routes they apply to.

`register_blueprint` wrapped each `url_value_preprocessor` in a closure that
compared the endpoint against the blueprint's prefix, then appended it to one
app-wide list. Every request then called every such closure — the
`O(blueprints * processors)` of no-op work the request-hook buckets were
introduced to avoid, four lines further down the same method.

It was worse than a scan. `is_bare` — the flag that decides whether a request
takes straight-line dispatch — listed `not app._url_value_preprocessors`, so a
single preprocessor on a single blueprint cost *every route in the app* its fast
path.

The processors are bucketed by dotted blueprint name now, the same way and by
the same code that buckets the hooks, and a nested blueprint's chain is
flattened at registration so a request is one dict lookup. Behaviour is
unchanged apart from ordering, which is stated below and tested.
"""

from __future__ import annotations

import pytest

from veloce import Blueprint, BuildError, Veloce
from veloce._pipeline import compile_pipeline
from veloce.testclient import TestClient


def _base() -> tuple[Veloce, list]:
    app = Veloce(openapi_url=None)
    seen: list = []
    return app, seen


# ── a processor applies to its own blueprint's routes ────────────────


def test_a_blueprint_processor_runs_for_its_own_route():
    app, seen = _base()
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def pull(endpoint, values):
        seen.append(endpoint)
        values.pop("locale", None)

    @bp.get("/{locale}/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    assert TestClient(app).get("/bp/en/x").status_code == 200
    assert seen == ["bp.x"]


def test_a_blueprint_processor_really_mutates_the_params():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def pull(endpoint, values):
        values["locale"] = values["locale"].upper()

    @bp.get("/{locale}/x")
    async def x(locale: str) -> dict:
        return {"locale": locale}

    app.register_blueprint(bp)
    assert TestClient(app).get("/bp/en/x").json() == {"locale": "EN"}


def test_a_blueprint_processor_does_not_run_for_an_app_route():
    """The scan's whole cost was on requests like this one."""
    app, seen = _base()
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def pull(endpoint, values):
        seen.append(endpoint)

    @bp.get("/x")
    async def x() -> dict:
        return {}

    @app.get("/plain")
    async def plain() -> dict:
        return {}

    app.register_blueprint(bp)
    TestClient(app).get("/plain")
    assert seen == []


def test_a_blueprint_processor_does_not_run_for_a_sibling():
    app, seen = _base()

    for name in ("a", "b"):
        bp = Blueprint(name, url_prefix=f"/{name}")

        @bp.url_value_preprocessor
        def pull(endpoint, values, _name=name):
            seen.append(_name)

        @bp.get("/x", name="x")
        async def x() -> dict:
            return {}

        app.register_blueprint(bp)

    TestClient(app).get("/a/x")
    assert seen == ["a"]


# ── nesting ──────────────────────────────────────────────────────────


def test_a_parent_processor_runs_for_a_nested_route():
    app, seen = _base()
    parent = Blueprint("p", url_prefix="/p")
    child = Blueprint("c", url_prefix="/c")

    @parent.url_value_preprocessor
    def from_parent(endpoint, values):
        seen.append("parent")

    @child.get("/x")
    async def x() -> dict:
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent)
    TestClient(app).get("/p/c/x")
    assert seen == ["parent"]


def test_a_child_processor_does_not_run_for_a_parent_route():
    app, seen = _base()
    parent = Blueprint("p", url_prefix="/p")
    child = Blueprint("c", url_prefix="/c")

    @child.url_value_preprocessor
    def from_child(endpoint, values):
        seen.append("child")

    @parent.get("/own")
    async def own() -> dict:
        return {}

    @child.get("/x")
    async def x() -> dict:
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent)
    TestClient(app).get("/p/own")
    assert seen == []


def test_a_child_processor_does_not_run_for_a_sibling_child():
    app, seen = _base()
    parent = Blueprint("p", url_prefix="/p")
    first = Blueprint("one", url_prefix="/one")
    second = Blueprint("two", url_prefix="/two")

    @first.url_value_preprocessor
    def from_first(endpoint, values):
        seen.append("one")

    for child in (first, second):

        @child.get("/x", name="x")
        async def x() -> dict:
            return {}

        parent.register_blueprint(child)

    app.register_blueprint(parent)
    TestClient(app).get("/p/two/x")
    assert seen == []


def test_the_nested_chain_runs_outermost_first():
    app, seen = _base()
    parent = Blueprint("p", url_prefix="/p")
    child = Blueprint("c", url_prefix="/c")

    @parent.url_value_preprocessor
    def from_parent(endpoint, values):
        seen.append("parent")

    @child.url_value_preprocessor
    def from_child(endpoint, values):
        seen.append("child")

    @child.get("/x")
    async def x() -> dict:
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent)
    TestClient(app).get("/p/c/x")
    assert seen == ["parent", "child"]


def test_a_three_deep_chain_runs_in_order():
    app, seen = _base()
    a, b, c = (Blueprint(n, url_prefix=f"/{n}") for n in ("a", "b", "c"))

    for bp, label in ((a, "a"), (b, "b"), (c, "c")):

        @bp.url_value_preprocessor
        def pull(endpoint, values, _label=label):
            seen.append(_label)

    @c.get("/x")
    async def x() -> dict:
        return {}

    b.register_blueprint(c)
    a.register_blueprint(b)
    app.register_blueprint(a)
    TestClient(app).get("/a/b/c/x")
    assert seen == ["a", "b", "c"]


# ── app-level processors are untouched ───────────────────────────────


def test_an_app_processor_runs_for_every_route():
    app, seen = _base()

    @app.url_value_preprocessor
    def pull(endpoint, values):
        seen.append(endpoint)

    @app.get("/plain")
    async def plain() -> dict:
        return {}

    bp = Blueprint("bp", url_prefix="/bp")

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    client = TestClient(app)
    client.get("/plain")
    client.get("/bp/x")
    assert seen == ["plain", "bp.x"]


def test_an_app_processor_runs_before_the_blueprint_chain():
    """Stated ordering: app-level first, then the blueprint's, as hooks do."""
    app, seen = _base()

    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def from_bp(endpoint, values):
        seen.append("bp")

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)

    # Registered after the blueprint, and still runs first.
    @app.url_value_preprocessor
    def from_app(endpoint, values):
        seen.append("app")

    TestClient(app).get("/bp/x")
    assert seen == ["app", "bp"]


# ── the fast path ────────────────────────────────────────────────────


def test_a_blueprint_processor_no_longer_costs_the_app_its_fast_path():
    """The defect: one preprocessor anywhere set `is_bare` False for everything."""
    app = Veloce(openapi_url=None)

    @app.get("/plain")
    async def plain() -> dict:
        return {}

    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def pull(endpoint, values):
        pass

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    assert compile_pipeline(app).is_bare is True


def test_an_app_level_processor_still_leaves_the_fast_path():
    """It applies to every endpoint, so the fast path genuinely cannot skip it."""
    app = Veloce(openapi_url=None)

    @app.url_value_preprocessor
    def pull(endpoint, values):
        pass

    @app.get("/plain")
    async def plain() -> dict:
        return {}

    assert compile_pipeline(app).is_bare is False


def test_the_fast_path_still_applies_a_blueprint_processor():
    """The correctness risk of keeping `is_bare`: it must not skip the work."""
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def upper(endpoint, values):
        values["locale"] = values["locale"].upper()

    @bp.get("/{locale}/x")
    async def x(request) -> dict:
        return {"locale": request.path_params["locale"]}

    app.register_blueprint(bp)
    assert compile_pipeline(app).is_bare is True
    assert TestClient(app).get("/bp/en/x").json() == {"locale": "EN"}


def test_the_bucket_is_none_when_no_blueprint_registered_one():
    """The fast path tests one attribute; it must be `None`, not `{}`."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert compile_pipeline(app).bp_url_procs is None


def test_the_bucket_is_populated_when_one_exists():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def pull(endpoint, values):
        pass

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    assert compile_pipeline(app).bp_url_procs == {"bp": [pull]}


# ── url_defaults, through url_for ────────────────────────────────────


def test_a_blueprint_url_default_applies_to_its_own_endpoint():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_defaults
    def add_locale(endpoint, values):
        values.setdefault("locale", "en")

    @bp.get("/{locale}/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    assert app.url_for("bp.x") == "/bp/en/x"


def test_a_blueprint_url_default_does_not_apply_to_another_endpoint():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_defaults
    def add_locale(endpoint, values):
        values.setdefault("locale", "en")

    @bp.get("/x")
    async def x() -> dict:
        return {}

    @app.get("/{locale}/plain")
    async def plain() -> dict:
        return {}

    app.register_blueprint(bp)
    with pytest.raises(BuildError, match="plain"):
        app.url_for("plain")


def test_an_app_url_default_still_applies_everywhere():
    app = Veloce(openapi_url=None)

    @app.url_defaults
    def add_locale(endpoint, values):
        values.setdefault("locale", "en")

    @app.get("/{locale}/plain")
    async def plain() -> dict:
        return {}

    bp = Blueprint("bp", url_prefix="/bp")

    @bp.get("/{locale}/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    assert app.url_for("plain") == "/en/plain"
    assert app.url_for("bp.x") == "/bp/en/x"


def test_a_nested_url_default_chain_runs_outermost_first():
    app = Veloce(openapi_url=None)
    order = []
    parent = Blueprint("p", url_prefix="/p")
    child = Blueprint("c", url_prefix="/c")

    @parent.url_defaults
    def from_parent(endpoint, values):
        order.append("parent")

    @child.url_defaults
    def from_child(endpoint, values):
        order.append("child")

    @child.get("/x")
    async def x() -> dict:
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent)
    app.url_for("p.c.x")
    assert order == ["parent", "child"]


def test_url_for_is_unaffected_when_nothing_is_registered():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert app.url_for("x") == "/x"


# ── the introspection views ──────────────────────────────────────────


def test_the_view_keys_app_processors_under_none():
    app = Veloce(openapi_url=None)

    @app.url_value_preprocessor
    def pull(endpoint, values):
        pass

    assert app.url_value_preprocessors == {None: [pull]}


def test_the_view_keys_a_blueprint_under_its_name():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def pull(endpoint, values):
        pass

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    assert app.url_value_preprocessors == {None: [], "bp": [pull]}


def test_the_view_shows_the_flattened_nested_chain():
    app = Veloce(openapi_url=None)
    parent = Blueprint("p", url_prefix="/p")
    child = Blueprint("c", url_prefix="/c")

    @parent.url_value_preprocessor
    def from_parent(endpoint, values):
        pass

    @child.url_value_preprocessor
    def from_child(endpoint, values):
        pass

    @child.get("/x")
    async def x() -> dict:
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent)
    assert app.url_value_preprocessors["p"] == [from_parent]
    assert app.url_value_preprocessors["p.c"] == [from_parent, from_child]


def test_the_view_is_a_copy():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def pull(endpoint, values):
        pass

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    app.url_value_preprocessors["bp"].append("junk")
    assert app._bp_url_value_preprocessors["bp"] == [pull]


# ── the shared helper the MCP path uses ──────────────────────────────


def test_the_helper_runs_the_blueprint_chain_too():
    """`_run_url_value_preprocessors` is the MCP tool path; it must agree."""
    app, seen = _base()
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def pull(endpoint, values):
        seen.append(endpoint)

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    app._run_url_value_preprocessors("bp.x", {})
    assert seen == ["bp.x"]


def test_the_helper_skips_a_foreign_endpoint():
    app, seen = _base()
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def pull(endpoint, values):
        seen.append(endpoint)

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    app._run_url_value_preprocessors("plain", {})
    app._run_url_value_preprocessors(None, {})
    assert seen == []


def test_the_helper_and_a_request_agree():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def upper(endpoint, values):
        values["locale"] = values["locale"].upper()

    @bp.get("/{locale}/x")
    async def x(request) -> dict:
        return {"locale": request.path_params["locale"]}

    app.register_blueprint(bp)
    via_helper: dict = {"locale": "en"}
    app._run_url_value_preprocessors("bp.x", via_helper)
    assert via_helper == {"locale": "EN"}
    assert TestClient(app).get("/bp/en/x").json() == {"locale": "EN"}


# ── registration bookkeeping ─────────────────────────────────────────


def test_registering_a_blueprint_twice_does_not_double_the_processors():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp")

    @bp.url_value_preprocessor
    def pull(endpoint, values):
        pass

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp, url_prefix="/v1")
    app.register_blueprint(bp, url_prefix="/v2")
    assert app._bp_url_value_preprocessors["bp"] == [pull]


def test_a_blueprint_with_no_processors_adds_no_bucket_entry():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    assert app._bp_url_value_preprocessors == {}


def test_several_processors_on_one_blueprint_run_in_registration_order():
    app, seen = _base()
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.url_value_preprocessor
    def first(endpoint, values):
        seen.append("first")

    @bp.url_value_preprocessor
    def second(endpoint, values):
        seen.append("second")

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    TestClient(app).get("/bp/x")
    assert seen == ["first", "second"]
