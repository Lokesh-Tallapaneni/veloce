"""Mounting one blueprint twice must not run its hooks twice.

`register_blueprint` documents this: "Re-registers each route under
`(url_prefix or bp.url_prefix) + path` so the same blueprint can be mounted
twice (e.g. v1/v2 versions)."

The routes were re-registered correctly. Everything else was not. Hooks are
bucketed by the blueprint's *name*, and both mounts give their routes the same
`<bpname>.` endpoint prefix — so one lookup found one bucket, and appending to it
a second time meant every hook ran twice on a single request. A rate-limit or
audit `before_request` double-counted; a URL value preprocessor rewrote the same
values twice.
"""

from __future__ import annotations

import pytest

from veloce import Blueprint, Veloce
from veloce.testclient import TestClient


def _mounted(*prefixes: str, hooks: bool = True):
    """Mount one blueprint at each prefix; return (client, fired-log)."""
    fired: list[str] = []
    app = Veloce(openapi_url=None)
    bp = Blueprint("api")

    if hooks:

        @bp.before_request
        async def before(request):
            fired.append("before")

        @bp.after_request
        async def after(request, response):
            fired.append("after")
            return response

        @bp.teardown_request
        async def teardown(exc):
            fired.append("teardown")

    @bp.get("/ping")
    async def ping():
        return {"ok": True}

    for prefix in prefixes:
        app.register_blueprint(bp, url_prefix=prefix)
    return TestClient(app), fired


# ── the defect ───────────────────────────────────────────────────────


def test_a_hook_runs_once_when_the_blueprint_is_mounted_twice():
    """The defect: every hook fired twice on one request."""
    client, fired = _mounted("/v1", "/v2")
    fired.clear()
    client.get("/v1/ping")
    assert fired == ["before", "after", "teardown"]


def test_the_second_mount_runs_its_hooks_once_too():
    client, fired = _mounted("/v1", "/v2")
    fired.clear()
    client.get("/v2/ping")
    assert fired == ["before", "after", "teardown"]


@pytest.mark.parametrize("mounts", [1, 2, 3, 5])
def test_the_hook_count_does_not_grow_with_the_number_of_mounts(mounts):
    prefixes = [f"/v{n}" for n in range(1, mounts + 1)]
    client, fired = _mounted(*prefixes)
    fired.clear()
    client.get("/v1/ping")
    assert fired.count("before") == 1


def test_a_counting_hook_counts_each_request_once():
    """The shape that made this matter: a rate limiter or an audit counter."""
    counted: list[str] = []
    app = Veloce(openapi_url=None)
    bp = Blueprint("api")

    @bp.before_request
    async def count(request):
        counted.append(request.path)

    @bp.get("/ping")
    async def ping():
        return {}

    app.register_blueprint(bp, url_prefix="/v1")
    app.register_blueprint(bp, url_prefix="/v2")

    client = TestClient(app)
    client.get("/v1/ping")
    client.get("/v2/ping")
    assert counted == ["/v1/ping", "/v2/ping"]


# ── what mounting twice is for: the routes ───────────────────────────


def test_both_mounts_serve():
    client, _ = _mounted("/v1", "/v2")
    assert client.get("/v1/ping").status_code == 200
    assert client.get("/v2/ping").status_code == 200


def test_both_mounts_serve_when_the_blueprint_has_no_hooks():
    client, _ = _mounted("/v1", "/v2", hooks=False)
    assert client.get("/v1/ping").status_code == 200
    assert client.get("/v2/ping").status_code == 200


def test_a_single_mount_is_unchanged():
    client, fired = _mounted("/v1")
    fired.clear()
    client.get("/v1/ping")
    assert fired == ["before", "after", "teardown"]


# ── URL processors: the same bucketing, the same doubling ────────────


def test_a_url_value_preprocessor_runs_once():
    seen: list[str] = []
    app = Veloce(openapi_url=None)
    bp = Blueprint("api")

    @bp.url_value_preprocessor
    def record(endpoint, values):
        seen.append(endpoint)

    @bp.get("/ping")
    async def ping():
        return {}

    app.register_blueprint(bp, url_prefix="/v1")
    app.register_blueprint(bp, url_prefix="/v2")

    TestClient(app).get("/v1/ping")
    assert seen == ["api.ping"]


def test_a_url_default_func_is_registered_once():
    app = Veloce(openapi_url=None)
    bp = Blueprint("api")

    @bp.url_defaults
    def add_default(endpoint, values):
        values.setdefault("lang", "en")

    @bp.get("/ping")
    async def ping():
        return {}

    before = len(app._url_default_funcs)
    app.register_blueprint(bp, url_prefix="/v1")
    after_one = len(app._url_default_funcs)
    app.register_blueprint(bp, url_prefix="/v2")
    assert len(app._url_default_funcs) == after_one
    assert after_one == before + 1


# ── two different blueprints are not affected ────────────────────────


def test_two_distinct_blueprints_each_keep_their_hooks():
    fired: list[str] = []
    app = Veloce(openapi_url=None)
    first = Blueprint("first", url_prefix="/a")
    second = Blueprint("second", url_prefix="/b")

    for bp, label in ((first, "first"), (second, "second")):

        @bp.before_request
        async def hook(request, _label=label):
            fired.append(_label)

        @bp.get("/ping")
        async def ping():
            return {}

    app.register_blueprint(first)
    app.register_blueprint(second)

    client = TestClient(app)
    client.get("/a/ping")
    assert fired == ["first"]
    fired.clear()
    client.get("/b/ping")
    assert fired == ["second"]


def test_a_nested_child_still_inherits_its_parent_hook_once():
    """Mounting the parent twice must not double the inherited chain either."""
    fired: list[str] = []
    app = Veloce(openapi_url=None)
    parent = Blueprint("parent")
    child = Blueprint("child", url_prefix="/c")

    @parent.before_request
    async def guard(request):
        fired.append("parent")

    @child.get("/x")
    async def x():
        return {}

    parent.register_blueprint(child)
    app.register_blueprint(parent, url_prefix="/v1")
    app.register_blueprint(parent, url_prefix="/v2")

    client = TestClient(app)
    client.get("/v1/c/x")
    assert fired == ["parent"]
    fired.clear()
    client.get("/v2/c/x")
    assert fired == ["parent"]


def test_the_blueprint_is_still_reachable_from_app_blueprints():
    client, _ = _mounted("/v1", "/v2")
    assert "api" in client.app.blueprints
