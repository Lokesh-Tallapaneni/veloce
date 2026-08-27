"""`Veloce.preprocess_request` / `process_response` must fire blueprint hooks
for routes the blueprint owns.

Inspector F-002 on PR #65: the bucket refactor moved blueprint hooks out
of the flat `_*_hooks` lists into `_bp_*_hooks` dicts. The dispatch path
walks the bucket; the public hook helpers
`preprocess_request` / `process_response` were missed and would silently
skip every blueprint-registered hook when called directly by user code,
extensions, or tests.
"""

from __future__ import annotations

from veloce import Veloce
from veloce.blueprints import Blueprint
from veloce.http.request import Request
from veloce.http.response import Response


def _make_request(endpoint: str) -> Request:
    req = Request(method="GET", path="/x", query_string="", headers={}, body=b"")
    req.endpoint = endpoint
    return req


def test_preprocess_request_runs_matched_blueprint_hooks():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")
    fired: list[str] = []

    @app.before_request
    def app_hook(request):
        fired.append("app")

    @bp.before_request
    def bp_hook(request):
        fired.append("bp")

    @bp.get("/x")
    def handler():
        return {"ok": True}

    app.register_blueprint(bp)

    import asyncio

    asyncio.run(app.preprocess_request(_make_request("bp.handler")))
    assert fired == ["app", "bp"]


def test_preprocess_request_skips_non_matching_blueprint():
    app = Veloce(openapi_url=None)
    bp_a = Blueprint("bp_a")
    bp_b = Blueprint("bp_b")
    fired: list[str] = []

    @bp_a.before_request
    def hook_a(request):
        fired.append("a")

    @bp_b.before_request
    def hook_b(request):
        fired.append("b")

    @bp_a.get("/x")
    def ha():
        return {}

    @bp_b.get("/y")
    def hb():
        return {}

    app.register_blueprint(bp_a)
    app.register_blueprint(bp_b)

    import asyncio

    asyncio.run(app.preprocess_request(_make_request("bp_a.ha")))
    assert fired == ["a"]


def test_process_response_runs_matched_blueprint_hooks_reversed():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp")
    fired: list[str] = []

    @app.after_request
    def app_after(request, response):
        fired.append("app")
        return None

    @bp.after_request
    def bp_after(request, response):
        fired.append("bp")
        return None

    @bp.get("/x")
    def handler():
        return {}

    app.register_blueprint(bp)

    import asyncio

    asyncio.run(app.process_response(_make_request("bp.handler"), Response()))
    # Reversed: app-level reverse-iterates first, then bp bucket reverse-iterates.
    assert fired == ["app", "bp"]


def test_blueprint_teardown_runs_when_resolve_exits_early_without_before_hooks():
    """A blueprint `teardown_request` still fires when `_resolve_route` short-circuits
    (here a subdomain mismatch -> 404) on an app with no before_request hooks. The
    dispatch fast path must still derive `bp_name` from the matched endpoint even
    when the (absent) before-hook coroutine is skipped, otherwise the finally-block
    teardown selection cannot find the blueprint's hook."""
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")
    fired: list[str] = []

    @bp.teardown_request
    def bp_teardown(exc):
        fired.append("bp")

    @bp.get("/x", subdomain="api")
    def handler():
        return {}

    app.register_blueprint(bp)

    import asyncio

    req = Request(
        method="GET",
        path="/bp/x",
        query_string="",
        headers={"host": "other.example.com"},
        body=b"",
    )
    resp = asyncio.run(app.handle_request(req))
    assert resp.status_code == 404
    # Teardown fired despite the early 404 return, because `bp_name` was derived.
    assert fired == ["bp"]
