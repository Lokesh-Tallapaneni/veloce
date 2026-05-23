"""`Veloce.preprocess_request` / `process_response` must fire blueprint hooks
for routes the blueprint owns.

Inspector F-002 on PR #65: the bucket refactor moved blueprint hooks out
of the flat `_*_hooks` lists into `_bp_*_hooks` dicts. The dispatch path
walks the bucket; the public Flask-shape helpers
`preprocess_request` / `process_response` were missed and would silently
skip every blueprint-registered hook when called directly by user code,
extensions, or tests.
"""

from __future__ import annotations

from veloce import Veloce
from veloce.blueprints import Blueprint
from veloce.http.request import Request


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

    from veloce.http.response import Response

    asyncio.run(app.process_response(_make_request("bp.handler"), Response()))
    # Reversed: app-level reverse-iterates first, then bp bucket reverse-iterates.
    assert fired == ["app", "bp"]
