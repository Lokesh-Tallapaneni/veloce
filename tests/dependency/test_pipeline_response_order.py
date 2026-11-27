"""Response-phase ordering snapshot - frozen across the PH_HTTP_POST fusion.

Pins the exact response-phase sequence the compiled pipeline must preserve:
app `after_request` (reverse), then blueprint `after_request` (reverse), then
`after_this_request` one-shots, then response middleware (reverse) - in that
order. Fusing the eight response-middleware call sites onto `cp.http_post` must
not reorder any stage relative to the after-hooks, so this records the observed
order and asserts it byte-for-byte.
"""

from __future__ import annotations

from veloce import Blueprint, Veloce
from veloce.helpers import after_this_request
from veloce.middleware.base import Middleware
from veloce.testclient import TestClient


def test_response_phase_order_after_hooks_then_middleware():
    order: list[str] = []

    class _RecordingMiddleware(Middleware):
        def __init__(self, tag: str) -> None:
            super().__init__(name=tag)
            self._tag = tag

        async def process_response(self, request, response):
            order.append(f"mw:{self._tag}")
            return response

    app = Veloce()
    app.config["TESTING"] = True

    # Two app-level after_request hooks - fire in REVERSE registration order.
    @app.after_request
    def _app_after_a(request, response):
        order.append("app_after:a")
        return response

    @app.after_request
    def _app_after_b(request, response):
        order.append("app_after:b")
        return response

    bp = Blueprint("bp", url_prefix="/bp")

    # Two blueprint after_request hooks - also REVERSE registration order, and
    # only after the app-level ones.
    @bp.after_request
    def _bp_after_a(request, response):
        order.append("bp_after:a")
        return response

    @bp.after_request
    def _bp_after_b(request, response):
        order.append("bp_after:b")
        return response

    @bp.get("/x")
    async def _handler(request):
        # A per-request one-shot - runs after both after_request stages.
        @after_this_request
        def _one_shot(request, response):
            order.append("after_this_request")
            return response

        return {"ok": True}

    app.register_blueprint(bp)

    # Two response middlewares - run LAST, in REVERSE registration order.
    app.add_middleware(_RecordingMiddleware("first"))
    app.add_middleware(_RecordingMiddleware("second"))

    client = TestClient(app)
    resp = client.get("/bp/x")
    assert resp.status_code == 200

    assert order == [
        # app after_request, reversed
        "app_after:b",
        "app_after:a",
        # blueprint after_request, reversed
        "bp_after:b",
        "bp_after:a",
        # one-shot
        "after_this_request",
        # response middleware, reversed
        "mw:second",
        "mw:first",
    ]
