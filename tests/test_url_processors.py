"""url_value_preprocessor + url_defaults hook tests (R20+R21)."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Request, Veloce, g


def _req(path: str) -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── url_value_preprocessor ────────────────────────────────────────────


async def test_preprocessor_pops_path_param_into_g():
    app = Veloce(debug=True, openapi_url=None)
    captured: dict = {}

    @app.url_value_preprocessor
    def pull_lang(endpoint, values):
        captured["endpoint"] = endpoint
        g.lang = values.pop("lang", "en")

    @app.get("/{lang}/users", name="users_index")
    async def users(request: Request):
        captured["path_params"] = dict(request.path_params)
        captured["lang"] = g.lang
        return {"ok": True}

    resp = await app.handle_request(_req("/fr/users"))
    assert resp.status_code == 200
    # `lang` was popped → not visible to the handler as a kwarg.
    assert captured["path_params"] == {}
    assert captured["lang"] == "fr"
    assert captured["endpoint"] == "users_index"


async def test_preprocessor_does_not_pop_means_handler_still_sees_value():
    """A preprocessor that only reads (no pop) leaves the value intact."""
    app = Veloce(debug=True, openapi_url=None)
    captured: dict = {}

    @app.url_value_preprocessor
    def trace(endpoint, values):
        captured["seen"] = dict(values)

    @app.get("/{tag}/items", name="items")
    async def items(tag: str):
        captured["tag"] = tag
        return {"tag": tag}

    await app.handle_request(_req("/red/items"))
    assert captured["seen"] == {"tag": "red"}
    assert captured["tag"] == "red"


async def test_multiple_preprocessors_run_in_registration_order():
    app = Veloce(debug=True, openapi_url=None)
    order: list[str] = []

    @app.url_value_preprocessor
    def first(endpoint, values):
        order.append("first")

    @app.url_value_preprocessor
    def second(endpoint, values):
        order.append("second")

    @app.get("/x", name="x")
    async def x():
        return {}

    await app.handle_request(_req("/x"))
    assert order == ["first", "second"]


# ── url_defaults ──────────────────────────────────────────────────────


def test_url_defaults_injects_kwargs():
    app = Veloce(debug=True, openapi_url=None)

    @app.url_defaults
    def add_lang(endpoint, values):
        values.setdefault("lang", "en")

    @app.get("/{lang}/users", name="users_index")
    async def users():
        return {}

    # Caller didn't pass `lang` — url_defaults injects it.
    assert app.url_for("users_index") == "/en/users"


def test_url_defaults_does_not_override_caller_value():
    """`setdefault` semantics — caller's explicit kwarg wins."""
    app = Veloce(debug=True, openapi_url=None)

    @app.url_defaults
    def add_lang(endpoint, values):
        values.setdefault("lang", "en")

    @app.get("/{lang}/users", name="users_index")
    async def users():
        return {}

    assert app.url_for("users_index", lang="fr") == "/fr/users"


def test_url_defaults_url_path_for_alias_also_honours_callbacks():
    """`url_path_for` (the alias) routes through the same Veloce
    override so it picks up the default kwargs too."""
    app = Veloce(debug=True, openapi_url=None)

    @app.url_defaults
    def add_lang(endpoint, values):
        values.setdefault("lang", "en")

    @app.get("/{lang}/users", name="users_index")
    async def users():
        return {}

    assert app.url_path_for("users_index") == "/en/users"


def test_multiple_url_defaults_run_in_registration_order():
    app = Veloce(debug=True, openapi_url=None)

    @app.url_defaults
    def first(endpoint, values):
        values["a"] = "first-a"

    @app.url_defaults
    def second(endpoint, values):
        # Second overwrites first's `a` (last writer wins; no setdefault).
        values["a"] = "second-a"

    @app.get("/{a}", name="route")
    async def route():
        return {}

    assert app.url_for("route") == "/second-a"


def test_no_processors_registered_is_unchanged():
    """With no callbacks, `url_for` behaves exactly as before."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/items/{id}", name="get_item")
    async def get_item():
        return {}

    assert app.url_for("get_item", id="42") == "/items/42"


# ── End-to-end interplay ─────────────────────────────────────────────


async def test_preprocessor_and_defaults_combine_for_i18n_pattern():
    """Common pattern: strip `lang` segment in preprocessor, re-inject
    in url_defaults so links built inside the handler are scoped."""
    app = Veloce(debug=True, openapi_url=None)

    @app.url_value_preprocessor
    def pull(endpoint, values):
        g.lang = values.pop("lang", "en")

    @app.url_defaults
    def inject(endpoint, values):
        # If the route's template still has `lang`, push the current value.
        template, _ = app._named_routes.get(endpoint, ("", []))
        if "{lang}" in template:
            values.setdefault("lang", g.get("lang", "en"))

    @app.get("/{lang}/items/{id}", name="show_item")
    async def show(id: str):
        # Handler doesn't need `lang`; preprocessor stripped it.
        # Building a link via url_for re-introduces it.
        return {"link": app.url_for("show_item", id=id)}

    resp = await app.handle_request(_req("/fr/items/7"))
    body = resp.body
    assert b'"/fr/items/7"' in body
