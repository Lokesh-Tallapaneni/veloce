"""MethodView class-based views."""

from __future__ import annotations

import pytest

from veloce import MethodView, Request, Veloce
from veloce.exceptions import MethodNotAllowed


def _req(path: str = "/x", method: str = "GET") -> Request:
    return Request(method=method, path=path, query_string="", headers={}, body=b"")


# ── as_view returns a dispatcher with the right metadata ─────────────


def test_as_view_attaches_metadata():
    class V(MethodView):
        async def get(self, request):
            return {"verb": "get"}

        async def post(self, request):
            return {"verb": "post"}

    view = V.as_view("v")
    assert view.__name__ == "v"
    assert view.view_class is V
    assert sorted(view.methods) == ["GET", "POST"]


def test_methods_class_override_wins():
    class V(MethodView):
        methods = ["GET"]

        async def get(self, request):
            return {}

        async def post(self, request):
            return {}

    view = V.as_view("v")
    assert view.methods == ["GET"]


# ── sync methods are rejected at class definition ───────────────────


def test_sync_method_rejected_at_definition():
    with pytest.raises(TypeError, match="must be async"):

        class V(MethodView):
            def get(self, request):  # sync — must fail
                return {}


# ── dispatch picks the right method, 405 otherwise ──────────────────


@pytest.mark.asyncio
async def test_dispatch_picks_matching_method():
    class V(MethodView):
        async def get(self, request):
            return {"ok": "get"}

        async def post(self, request):
            return {"ok": "post"}

    instance = V()
    result = await instance.dispatch_request(_req(method="POST"))
    assert result == {"ok": "post"}


@pytest.mark.asyncio
async def test_dispatch_missing_method_raises_405_with_allow_header():
    class V(MethodView):
        async def get(self, request):
            return {}

    instance = V()
    with pytest.raises(MethodNotAllowed) as exc:
        await instance.dispatch_request(_req(method="POST"))
    assert exc.value.headers["Allow"] == "GET"


# ── Round trip through Veloce ────────────────────────────────────────


@pytest.mark.asyncio
async def test_round_trip_through_app():
    app = Veloce(debug=True, openapi_url=None)

    class UserView(MethodView):
        async def get(self, request):
            return {"verb": "GET"}

        async def post(self, request):
            return {"verb": "POST"}

    app.add_url_rule("/u", view_func=UserView.as_view("user"), methods=["GET", "POST"])

    resp_get = await app.handle_request(_req("/u", "GET"))
    import orjson

    assert orjson.loads(resp_get.body) == {"verb": "GET"}

    resp_post = await app.handle_request(_req("/u", "POST"))
    assert orjson.loads(resp_post.body) == {"verb": "POST"}


@pytest.mark.asyncio
async def test_fresh_instance_per_request():
    """Each request constructs a new instance — request-state isolation."""
    seen: list[int] = []

    class V(MethodView):
        def __init__(self):
            self.id = id(self)

        async def get(self, request):
            seen.append(self.id)
            return {}

    app = Veloce(debug=True, openapi_url=None)
    app.add_url_rule("/x", view_func=V.as_view("v"))

    await app.handle_request(_req("/x"))
    await app.handle_request(_req("/x"))
    assert len(seen) == 2
    assert seen[0] != seen[1]
