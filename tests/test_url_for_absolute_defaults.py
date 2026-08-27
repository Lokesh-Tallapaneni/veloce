"""`url_for(external=True)` takes its host and scheme from a declared hook.

`Router.url_for` used to ask `hasattr(self, "config")` and read
`SERVER_NAME` / `PREFERRED_URL_SCHEME` off it - a base class testing for an
attribute only its `Veloce` subclass defines. The dependency ran the wrong way:
neither a type checker nor a reader of `Router` could see the relationship, and
a second `Router` subclass had no way to participate except by happening to be
called `config`.

`_absolute_url_defaults()` is the seam now. `Router` answers "no opinion"
(`None`, `http`); `Veloce` answers from its configuration.
"""

from __future__ import annotations

from veloce import Router, Veloce

# ── a bare Router has no configuration, and says so ──────────────────


def test_a_bare_router_defaults_to_localhost_over_http():
    router = Router()

    @router.get("/x", name="x")
    async def x():
        return {}

    assert router.url_for("x", _external=True) == "http://localhost/x"


def test_the_hook_reports_no_opinion_on_a_bare_router():
    assert Router()._absolute_url_defaults() == (None, "http")


def test_an_explicit_host_still_wins_on_a_bare_router():
    router = Router()

    @router.get("/x", name="x")
    async def x():
        return {}

    assert router.url_for("x", _external=True, _host="example.com") == "http://example.com/x"


# ── an application answers from its configuration ────────────────────


def test_an_app_uses_server_name_and_scheme():
    app = Veloce(openapi_url=None)
    app.config["SERVER_NAME"] = "api.example.com"
    app.config["PREFERRED_URL_SCHEME"] = "https"

    @app.get("/x", name="x")
    async def x():
        return {}

    assert app.url_for("x", _external=True) == "https://api.example.com/x"


def test_the_hook_reads_the_apps_config():
    app = Veloce(openapi_url=None)
    app.config["SERVER_NAME"] = "api.example.com"
    app.config["PREFERRED_URL_SCHEME"] = "https"
    assert app._absolute_url_defaults() == ("api.example.com", "https")


def test_an_app_without_server_name_falls_back_to_localhost():
    app = Veloce(openapi_url=None)

    @app.get("/x", name="x")
    async def x():
        return {}

    assert app.url_for("x", _external=True) == "http://localhost/x"


def test_an_explicit_scheme_overrides_the_configured_one():
    app = Veloce(openapi_url=None)
    app.config["PREFERRED_URL_SCHEME"] = "https"

    @app.get("/x", name="x")
    async def x():
        return {}

    assert app.url_for("x", _external=True, _scheme="http").startswith("http://")


# ── and a third subclass can participate ─────────────────────────────


def test_another_router_subclass_can_supply_its_own_defaults():
    """What the `hasattr` shape made impossible without being named `config`."""

    class PinnedRouter(Router):
        def _absolute_url_defaults(self) -> tuple[str | None, str]:
            return "pinned.example", "https"

    router = PinnedRouter()

    @router.get("/x", name="x")
    async def x():
        return {}

    assert router.url_for("x", _external=True) == "https://pinned.example/x"


# ── the relative form is untouched ───────────────────────────────────


def test_a_relative_url_does_not_consult_the_hook():
    """The negative: the hook must not leak into the common case."""

    class Exploding(Router):
        def _absolute_url_defaults(self) -> tuple[str | None, str]:
            raise AssertionError("consulted for a relative URL")

    router = Exploding()

    @router.get("/x", name="x")
    async def x():
        return {}

    assert router.url_for("x") == "/x"
