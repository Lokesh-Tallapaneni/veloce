"""L7 — per-blueprint url_value_preprocessor + url_defaults."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Blueprint, Request, Veloce, g


def _req(path: str = "/api/x/page") -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── url_value_preprocessor ───────────────────────────────────────────


async def test_blueprint_preprocessor_runs_for_blueprint_routes():
    bp = Blueprint("api", url_prefix="/api")
    seen: list[dict] = []

    @bp.url_value_preprocessor
    def pop_lang(endpoint, values):
        # Pop `lang` from path params into g.
        if "lang" in values:
            g.lang = values.pop("lang")
            seen.append({"endpoint": endpoint, "lang": g.lang})

    @bp.get("/{lang}/page")
    async def page():
        return {"lang": g.get("lang")}

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(bp)

    import orjson

    resp = await app.handle_request(_req("/api/en/page"))
    assert orjson.loads(resp.body) == {"lang": "en"}
    assert seen and seen[0]["lang"] == "en"


async def test_blueprint_preprocessor_does_not_fire_on_other_routes():
    bp = Blueprint("api", url_prefix="/api")
    fired: list[str] = []

    @bp.url_value_preprocessor
    def trace(endpoint, values):
        fired.append(endpoint)

    @bp.get("/x")
    async def x():
        return {}

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(bp)

    @app.get("/other")
    async def other():
        return {}

    await app.handle_request(_req("/api/x"))
    await app.handle_request(_req("/other"))
    # Only the blueprint route invoked the preprocessor.
    assert fired == ["api.x"]


# ── url_defaults ─────────────────────────────────────────────────────


def test_blueprint_url_defaults_inject_value_for_url_for():
    bp = Blueprint("api", url_prefix="/api")

    @bp.url_defaults
    def add_lang(endpoint, values):
        values.setdefault("lang", "en")

    @bp.get("/{lang}/page", name="page")
    async def page():
        return {}

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(bp)

    # url_for resolves the route named `api.page`. The url_defaults
    # callback injects `lang="en"` so the caller doesn't need to.
    url = app.url_for("api.page")
    assert url == "/api/en/page"


def test_blueprint_url_defaults_skipped_for_other_endpoints():
    """Defaults registered on bpA don't bleed into bpB."""
    bp_a = Blueprint("a", url_prefix="/a")
    bp_b = Blueprint("b", url_prefix="/b")

    @bp_a.url_defaults
    def a_defaults(endpoint, values):
        values["from_a"] = "yes"  # would break /b/foo if it leaked

    @bp_b.get("/foo", name="foo")
    async def foo():
        return {}

    app = Veloce(debug=True, openapi_url=None)
    app.register_blueprint(bp_a)
    app.register_blueprint(bp_b)

    # Resolving b.foo must NOT pick up bpA's defaults (which would
    # append `?from_a=yes` to the URL).
    url = app.url_for("b.foo")
    assert url == "/b/foo"
