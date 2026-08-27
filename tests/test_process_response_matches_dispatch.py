"""`app.process_response` runs the after-request hooks the way serving does.

It was a second implementation of `_run_after_hooks` — the same loop, written
out again — and the two had drifted apart in three ways.

Dispatch calls `_call_after_hook`, which reads the hook's signature and passes
only the arguments it declares; `_after_hook_sig_cache` exists precisely because
passing both unconditionally turned `async def hook(response)` into a 500. The
alias passed both unconditionally, so that hook raised `TypeError` here while
serving fine in production. Dispatch keeps a hook's return only when it is a
`Response`; the alias kept anything non-`None`, so a hook that returned a dict
by accident replaced the response with the dict. And dispatch drains the
request's one-shot `after_this_request` callbacks; the alias skipped them.

A public alias that disagrees with the path it aliases is a trap: it is reached
for by exactly the people trying to verify a hook before shipping it. It now
calls `_run_after_hooks` rather than restating it.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from veloce import Blueprint, Request, Response, Veloce
from veloce.helpers import _current_request_var, after_this_request
from veloce.testclient import TestClient


def _request(endpoint: str | None = "x") -> Request:
    request = Request(method="GET", path="/x", query_string="", headers={}, body=b"")
    request.endpoint = endpoint
    return request


def _response() -> Response:
    return Response(body=b"{}", content_type="application/json")


# ── the signature adaptation ─────────────────────────────────────────


async def test_a_hook_taking_only_response_is_called():
    """The defect: this raised TypeError while serving fine in production."""
    app = Veloce(openapi_url=None)

    @app.after_request
    async def stamp(response):
        response.headers["X-Seen"] = "1"
        return response

    result = await app.process_response(_request(), _response())
    assert result.headers["X-Seen"] == "1"


async def test_a_hook_taking_only_request_is_called():
    app = Veloce(openapi_url=None)
    seen = []

    @app.after_request
    async def note(request):
        seen.append(request.path)

    await app.process_response(_request(), _response())
    assert seen == ["/x"]


async def test_a_hook_taking_both_is_called():
    app = Veloce(openapi_url=None)

    @app.after_request
    async def stamp(request, response):
        response.headers["X-Path"] = request.path
        return response

    result = await app.process_response(_request(), _response())
    assert result.headers["X-Path"] == "/x"


async def test_a_hook_taking_neither_is_called():
    app = Veloce(openapi_url=None)
    calls = []

    @app.after_request
    async def bare():
        calls.append(1)

    await app.process_response(_request(), _response())
    assert calls == [1]


async def test_a_hook_taking_kwargs_gets_both():
    app = Veloce(openapi_url=None)
    seen = {}

    @app.after_request
    async def catch_all(**kwargs):
        seen.update(kwargs)

    await app.process_response(_request(), _response())
    assert set(seen) == {"request", "response"}


async def test_a_sync_hook_is_supported():
    app = Veloce(openapi_url=None)

    @app.after_request
    def stamp(response):
        response.headers["X-Sync"] = "1"
        return response

    result = await app.process_response(_request(), _response())
    assert result.headers["X-Sync"] == "1"


# ── only a Response replaces the response ────────────────────────────


async def test_a_non_response_return_is_ignored():
    """The defect: a dict return became the response."""
    app = Veloce(openapi_url=None)

    @app.after_request
    async def stray(request, response):
        return {"not": "a response"}

    original = _response()
    assert await app.process_response(_request(), original) is original


@pytest.mark.parametrize("returned", [{"a": 1}, [1, 2], "text", 42, True, b"bytes"])
async def test_no_stray_return_type_replaces_the_response(returned):
    app = Veloce(openapi_url=None)

    @app.after_request
    async def stray(response):
        return returned

    original = _response()
    assert await app.process_response(_request(), original) is original


async def test_a_response_return_does_replace_it():
    app = Veloce(openapi_url=None)
    replacement = Response(body=b"replaced", content_type="text/plain")

    @app.after_request
    async def swap(response):
        return replacement

    assert await app.process_response(_request(), _response()) is replacement


async def test_a_none_return_keeps_the_response():
    app = Veloce(openapi_url=None)

    @app.after_request
    async def mutate(response):
        response.headers["X-Mutated"] = "1"

    result = await app.process_response(_request(), _response())
    assert result.headers["X-Mutated"] == "1"


# ── ordering ─────────────────────────────────────────────────────────


async def test_app_hooks_run_in_reverse_registration_order():
    app = Veloce(openapi_url=None)
    order = []

    @app.after_request
    async def first(response):
        order.append("first")

    @app.after_request
    async def second(response):
        order.append("second")

    await app.process_response(_request(), _response())
    assert order == ["second", "first"]


async def test_blueprint_hooks_run_after_the_app_hooks():

    app = Veloce(openapi_url=None)
    order = []

    @app.after_request
    async def app_hook(response):
        order.append("app")

    bp = Blueprint("bp", url_prefix="/bp")

    @bp.after_request
    async def bp_hook(response):
        order.append("bp")

    @bp.get("/x")
    async def x() -> dict:
        return {}

    app.register_blueprint(bp)
    await app.process_response(_request("bp.x"), _response())
    assert order == ["app", "bp"]


async def test_another_blueprints_hook_does_not_run():

    app = Veloce(openapi_url=None)
    ran = []

    for name in ("a", "b"):
        bp = Blueprint(name, url_prefix=f"/{name}")

        @bp.after_request
        async def hook(response, _name=name):
            ran.append(_name)

        @bp.get("/x", name="x")
        async def x() -> dict:
            return {}

        app.register_blueprint(bp)

    await app.process_response(_request("a.x"), _response())
    assert ran == ["a"]


async def test_an_endpoint_with_no_blueprint_runs_only_app_hooks():
    app = Veloce(openapi_url=None)
    ran = []

    @app.after_request
    async def app_hook(response):
        ran.append("app")

    await app.process_response(_request("x"), _response())
    assert ran == ["app"]


async def test_a_missing_endpoint_attribute_is_tolerated():
    """The alias is reached for with a hand-built request; it must not crash."""
    app = Veloce(openapi_url=None)
    ran = []

    @app.after_request
    async def app_hook(response):
        ran.append("app")

    await app.process_response(_request(None), _response())
    assert ran == ["app"]


async def test_no_hooks_returns_the_response_unchanged():
    app = Veloce(openapi_url=None)
    original = _response()
    assert await app.process_response(_request(), original) is original


# ── the one-shot callbacks ───────────────────────────────────────────


async def test_a_one_shot_callback_runs():
    """The defect: dispatch drains these and the alias skipped them."""
    app = Veloce(openapi_url=None)
    request = _request()
    ran = []

    token = _current_request_var.set(request)
    try:
        after_this_request(lambda response: ran.append("one-shot"))
    finally:
        _current_request_var.reset(token)

    await app.process_response(request, _response())
    assert ran == ["one-shot"]


async def test_a_one_shot_callback_runs_after_the_global_hooks():
    app = Veloce(openapi_url=None)
    order = []

    @app.after_request
    async def global_hook(response):
        order.append("global")

    request = _request()

    token = _current_request_var.set(request)
    try:
        after_this_request(lambda response: order.append("one-shot"))
    finally:
        _current_request_var.reset(token)

    await app.process_response(request, _response())
    assert order == ["global", "one-shot"]


# ── the alias agrees with a real request ─────────────────────────────


def _hooked_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.after_request
    async def only_response(response):
        response.headers["X-Seen"] = "1"
        return response

    @app.after_request
    async def strays(request, response):
        return {"not": "a response"}

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    return app


def test_a_real_request_is_served_by_the_same_hooks():
    response = TestClient(_hooked_app()).get("/x")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["X-Seen"] == "1"


def test_the_alias_produces_what_the_request_produces():
    """The property the fix is for: verify a hook here, ship it with confidence."""

    app = _hooked_app()
    served = TestClient(app).get("/x")

    # `asyncio.run` rather than an `async def` test: the sync `TestClient`
    # above drives its own loop, which cannot run inside a running one.
    result = asyncio.run(app.process_response(_request(), _response()))
    assert isinstance(result, Response)
    assert result.headers["X-Seen"] == served.headers["X-Seen"]


def test_the_alias_is_not_a_second_implementation():
    """A guard: restating the loop is how the two drifted the first time."""
    source = inspect.getsource(Veloce.process_response)
    assert "_run_after_hooks" in source
    assert "for hook in reversed" not in source
