"""Registering app-level behaviour takes the app off the straight-line fast path.

`_dispatch_request` has a fast path that skips the request-phase middleware,
the before/after hooks, route re-resolution and the dependency resolver, and
runs the handler directly. It is gated on `cp.is_bare` — a hand-written
conjunction of fourteen terms in `_pipeline.py`, one per thing the fast path
would otherwise skip.

A conjunction is only as complete as whoever last edited it. A new app-level
feature whose author does not add a term is not *broken loudly* — it is
**silently skipped on every request to a fast-eligible route**, and served
correctly on every other route, which is close to the worst shape a bug can
have.

These tests do not check the conjunction. They check its consequence: for each
public way of registering app-level behaviour, register one, hit a route that
*is* fast-eligible, and assert the behaviour happened. A term missing from
`is_bare` fails here as a hook that did not run.

A route is fast-eligible only when it is trivial — no parameters, or `request`
alone — so every route below is deliberately shaped that way. A test that used a
parameterised route would pass whether or not the fast path was taken, and would
be worthless.
"""

from __future__ import annotations

import pytest

from veloce import Blueprint, Middleware, Request, Response, Veloce
from veloce.testclient import TestClient


def _app() -> Veloce:
    """An app whose one route is fast-path eligible."""
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    return app


def _is_fast_eligible(app: Veloce) -> bool:
    return app.match("GET", "/").route_info.is_fast_eligible


def _bare(app: Veloce) -> bool:
    return app._ensure_pipeline().is_bare


# ── the premise ──────────────────────────────────────────────────────
#
# If these two stop holding, every test below passes vacuously.


def test_the_plain_route_is_fast_eligible():
    assert _is_fast_eligible(_app())


def test_the_plain_app_takes_the_fast_path():
    assert _bare(_app())


def test_the_plain_route_still_answers():
    assert TestClient(_app()).get("/").json() == {"ok": True}


# ── each registration must be observed ───────────────────────────────


def test_request_middleware_runs():
    seen = []
    app = _app()

    class M(Middleware):
        async def process_request(self, request):
            seen.append("req")
            return None

    app.add_middleware(M())
    TestClient(app).get("/")
    assert seen == ["req"]


def test_response_middleware_runs():
    seen = []
    app = _app()

    class M(Middleware):
        async def process_response(self, request, response):
            seen.append("resp")
            return response

    app.add_middleware(M())
    TestClient(app).get("/")
    assert seen == ["resp"]


def test_a_short_circuiting_middleware_is_honoured():
    """The strongest form: the handler must not run at all."""
    ran = []
    app = Veloce(openapi_url=None)

    class Block(Middleware):
        async def process_request(self, request):
            return Response(body=b"no", status_code=403)

    app.add_middleware(Block())

    @app.get("/")
    async def index():
        ran.append(1)
        return {"ok": True}

    assert TestClient(app).get("/").status_code == 403
    assert ran == []


def test_a_before_request_hook_runs():
    seen = []
    app = _app()

    @app.before_request
    async def before(request):
        seen.append("before")

    TestClient(app).get("/")
    assert seen == ["before"]


def test_a_before_request_short_circuit_is_honoured():
    ran = []
    app = Veloce(openapi_url=None)

    @app.before_request
    async def before(request):
        return Response(body=b"no", status_code=403)

    @app.get("/")
    async def index():
        ran.append(1)
        return {"ok": True}

    assert TestClient(app).get("/").status_code == 403
    assert ran == []


def test_an_after_request_hook_runs():
    seen = []
    app = _app()

    @app.after_request
    async def after(request, response):
        seen.append("after")
        return response

    TestClient(app).get("/")
    assert seen == ["after"]


def test_an_after_request_hook_can_rewrite_the_response():
    app = _app()

    @app.after_request
    async def after(request, response):
        response.headers["X-Seen"] = "yes"
        return response

    assert TestClient(app).get("/").headers["X-Seen"] == "yes"


def test_a_teardown_request_hook_runs():
    seen = []
    app = _app()

    @app.teardown_request
    async def teardown(exc):
        seen.append("teardown")

    TestClient(app).get("/")
    assert seen == ["teardown"]


def test_a_teardown_appcontext_hook_runs():
    seen = []
    app = _app()

    @app.teardown_appcontext
    async def teardown(exc):
        seen.append("appcontext")

    TestClient(app).get("/")
    assert seen == ["appcontext"]


def test_a_url_value_preprocessor_runs():
    seen = []
    app = _app()

    @app.url_value_preprocessor
    def preprocess(endpoint, values):
        seen.append(endpoint)

    TestClient(app).get("/")
    assert seen and seen[0] is not None


# ── blueprint-scoped registrations ───────────────────────────────────


def _blueprint_app():
    app = Veloce(openapi_url=None)
    bp = Blueprint("shop")

    @bp.get("/")
    async def index():
        return {"ok": True}

    return app, bp


def test_a_blueprint_before_hook_runs():
    seen = []
    app, bp = _blueprint_app()

    @bp.before_request
    async def before(request):
        seen.append("bp-before")

    app.register_blueprint(bp)
    TestClient(app).get("/")
    assert seen == ["bp-before"]


def test_a_blueprint_after_hook_runs():
    seen = []
    app, bp = _blueprint_app()

    @bp.after_request
    async def after(request, response):
        seen.append("bp-after")
        return response

    app.register_blueprint(bp)
    TestClient(app).get("/")
    assert seen == ["bp-after"]


def test_a_blueprint_teardown_hook_runs():
    seen = []
    app, bp = _blueprint_app()

    @bp.teardown_request
    async def teardown(exc):
        seen.append("bp-teardown")

    app.register_blueprint(bp)
    TestClient(app).get("/")
    assert seen == ["bp-teardown"]


def test_a_blueprint_url_value_preprocessor_runs():
    """Blueprint-scoped, so it deliberately does *not* un-bare the whole app -
    it is reached through `cp.bp_url_procs` on the fast path instead."""
    seen = []
    app, bp = _blueprint_app()

    @bp.url_value_preprocessor
    def preprocess(endpoint, values):
        seen.append(endpoint)

    app.register_blueprint(bp)
    TestClient(app).get("/")
    assert seen


# ── mounts ───────────────────────────────────────────────────────────


def test_a_mounted_sub_app_is_reachable():
    app = _app()
    sub = Veloce(openapi_url=None)

    @sub.get("/x")
    async def x():
        return {"from": "sub"}

    app.mount("/sub", sub)
    client = TestClient(app)
    assert client.get("/sub/x").json() == {"from": "sub"}
    assert client.get("/").json() == {"ok": True}


def test_a_static_mount_is_reachable(tmp_path):
    (tmp_path / "f.txt").write_text("S")
    app = _app()
    app.mount_static("/assets", str(tmp_path), must_exist=False)
    client = TestClient(app)
    assert client.get("/assets/f.txt").body == b"S"
    assert client.get("/").json() == {"ok": True}


def test_an_asgi_mount_is_reachable():
    app = _app()

    async def plain(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app.mount("/ext", plain)
    assert TestClient(app).get("/ext/anything").status_code == 204


# ── each registration also flips the flag ────────────────────────────
#
# The mechanism, asserted separately from the consequence: if a registration
# were somehow observed *without* leaving the fast path, that would be a
# different (and more interesting) bug than the one this file guards.


@pytest.mark.parametrize(
    "register",
    [
        pytest.param(lambda app: app.add_middleware(Middleware()), id="middleware"),
        pytest.param(lambda app: app.before_request(lambda request: None), id="before_request"),
        pytest.param(
            lambda app: app.after_request(lambda request, response: response), id="after_request"
        ),
        pytest.param(lambda app: app.teardown_request(lambda exc: None), id="teardown_request"),
        pytest.param(
            lambda app: app.teardown_appcontext(lambda exc: None), id="teardown_appcontext"
        ),
        pytest.param(
            lambda app: app.url_value_preprocessor(lambda endpoint, values: None),
            id="url_value_preprocessor",
        ),
        pytest.param(lambda app: app.mount("/sub", Veloce(openapi_url=None)), id="mount"),
    ],
)
def test_registering_leaves_the_fast_path(register):
    app = _app()
    assert _bare(app)
    register(app)
    assert not _bare(app), "registered app-level behaviour but `is_bare` stayed True"


def test_an_exception_handler_does_not_need_to_leave_the_fast_path():
    """The exception ladder surrounds the fast path rather than being skipped by
    it, so a handler is reached either way - asserted, not assumed."""
    app = Veloce(openapi_url=None)

    @app.get("/boom")
    async def boom():
        raise ValueError("x")

    @app.exception_handler(ValueError)
    async def handle(request, exc):
        return Response(body=b"handled", status_code=418)

    assert TestClient(app).get("/boom").status_code == 418


def test_a_request_only_route_is_still_fast_eligible():
    """`request`-alone is the other fast-eligible shape; it must stay one."""
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index(request: Request):
        return {"path": request.path}

    assert _is_fast_eligible(app)
    assert TestClient(app).get("/").json() == {"path": "/"}


# ── what a plugin can reach ──────────────────────────────────────────
#
# `_features` - the zero-overhead feature registry `is_bare` derives its four
# fused-phase terms from - is private, so an `app.install()` plugin cannot
# register a `FeatureSpec`. That is the design: the registry exists so a
# *built-in* optional feature costs a disabled app nothing, and its contract is
# internal. What matters is that the sanctioned extension mechanism still works
# on a fast-eligible route, through the public API, which is what these check.


def test_a_plugin_middleware_is_observed_on_a_fast_eligible_route():
    seen = []

    class Recording:
        name = "recorder"

        def install(self, app):
            class M(Middleware):
                async def process_request(self, request):
                    seen.append("plugin")
                    return None

            app.add_middleware(M())

    app = _app()
    app.install(Recording())
    TestClient(app).get("/")
    assert seen == ["plugin"]


def test_a_plugin_hook_is_observed_on_a_fast_eligible_route():
    seen = []

    class Hooking:
        name = "hooker"

        def install(self, app):
            @app.before_request
            async def before(request):
                seen.append("plugin-hook")

    app = _app()
    app.install(Hooking())
    TestClient(app).get("/")
    assert seen == ["plugin-hook"]


def test_a_plugin_takes_the_app_off_the_fast_path():
    class Hooking:
        name = "hooker"

        def install(self, app):
            app.before_request(lambda request: None)

    app = _app()
    assert _bare(app)
    app.install(Hooking())
    assert not _bare(app)


def test_a_plugin_that_registers_nothing_leaves_the_fast_path_intact():
    """The negative: installing must not cost an app its fast path by itself."""

    class Inert:
        name = "inert"

        def install(self, app):
            app.extensions["inert"] = True

    app = _app()
    app.install(Inert())
    assert _bare(app)
    assert TestClient(app).get("/").json() == {"ok": True}
