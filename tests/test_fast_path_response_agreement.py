"""The straight-line fast path may skip `_build_response`, and must keep doing so.

`_dispatch_request`'s fast branch used to call `_build_response`, which does four
things. On this path three of them are provably dead:

* `response_model` is applied — but `is_fast_eligible` requires
  `response_model is None`;
* `response_class` is passed to the coercion — but it requires
  `response_class is None`;
* a route-level `status_code` override is applied — but it requires
  `status_code == HTTP_200_OK`;
* a handler-injected `Response` is merged — and the *only* writers of
  `STATE_INJECTED_RESPONSE` are the `K_RESPONSE` slot resolver and its compiled
  equivalent, while `is_fast_eligible` requires a trivial or request-only plan,
  which by definition has no `Response` slot and no dependencies.

So the branch now calls `_coerce_response(result)` directly.

That is only safe while those implications hold, and they are a property of a
predicate ten lines long that someone will eventually loosen. This module pins
the implication itself: **for every fast-eligible route, the reduced call and
the full `_build_response` must produce the same response.** Loosen the
predicate without revisiting the fast path and these fail.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from veloce import Response, Veloce, status
from veloce.testclient import TestClient


class Item(BaseModel):
    name: str


# ── the implications `is_fast_eligible` is relied on for ─────────────


def _routes(app: Veloce):
    return [(m, p, i) for m, p, i in app._collect_all_routes(include_hidden=True)]


def _fast_eligible(app: Veloce):
    return [(m, p, i) for m, p, i in _routes(app) if i.is_fast_eligible]


def _kitchen_sink() -> Veloce:
    """One app carrying every route shape the fast path must classify."""
    app = Veloce(openapi_url=None)

    @app.get("/trivial")
    async def trivial():
        return {"a": 1}

    @app.get("/request-only")
    async def request_only(request):
        return {"b": 2}

    @app.get("/with-model", response_model=Item)
    async def with_model():
        return {"name": "x"}

    @app.get("/with-status", status_code=status.HTTP_201_CREATED)
    async def with_status():
        return {"c": 3}

    @app.get("/with-response-class", response_class=Response)
    async def with_response_class():
        return "raw"

    @app.get("/with-injected")
    async def with_injected(response: Response):
        response.status_code = 202
        return {"d": 4}

    @app.get("/with-params")
    async def with_params(q: int = 1):
        return {"q": q}

    def sync_handler():
        return {"sync": True}

    app.add_route("/sync", sync_handler, methods=["GET"])
    return app


@pytest.mark.parametrize(
    "attribute",
    ["response_model", "response_class"],
)
def test_a_fast_eligible_route_has_no(attribute):
    """The two implications the coercion call relies on."""
    for _m, path, info in _fast_eligible(_kitchen_sink()):
        assert getattr(info, attribute) is None, (path, attribute)


def test_a_fast_eligible_route_has_the_default_status():
    for _m, path, info in _fast_eligible(_kitchen_sink()):
        assert info.status_code == status.HTTP_200_OK, path


def test_a_fast_eligible_route_has_no_response_slot_and_no_dependencies():
    """The implication the injection merge relies on, checked at the plan.

    `STATE_INJECTED_RESPONSE` is written only by the `Response`-slot resolver,
    so a plan with no slots (trivial) or exactly one `K_REQUEST` slot
    (request-only) can never have it set.
    """
    from veloce._handler_plan import K_REQUEST

    for _m, path, info in _fast_eligible(_kitchen_sink()):
        slots = info.handler_plan.slots
        assert info.is_trivial_plan or info.is_request_only_plan, path
        assert all(slot.kind == K_REQUEST for slot in slots), path
        assert len(slots) <= 1, path


def test_the_classification_is_not_vacuous():
    """Every assertion above is trivially true if nothing is fast-eligible."""
    app = _kitchen_sink()
    eligible = {p for _m, p, _i in _fast_eligible(app)}
    assert "/trivial" in eligible
    assert "/request-only" in eligible


def test_the_excluded_shapes_really_are_excluded():
    """The negative: a route with any of the features would break the reduction."""
    app = _kitchen_sink()
    eligible = {p for _m, p, _i in _fast_eligible(app)}
    for path in (
        "/with-model",
        "/with-status",
        "/with-response-class",
        "/with-injected",
        "/sync",
    ):
        assert path not in eligible, path


# ── and the two calls agree on every eligible route ──────────────────


@pytest.mark.parametrize(
    ("path", "result"),
    [
        ("/trivial", {"a": 1}),
        ("/request-only", {"b": 2}),
    ],
)
def test_the_reduced_call_matches_the_full_builder(path, result):
    """The property, stated directly: on a fast-eligible route the reduced
    coercion and `_build_response` must produce the same response."""
    app = _kitchen_sink()
    _m, _p, info = next((m, p, i) for m, p, i in _routes(app) if p == path)

    class _Match:
        route_info = info

    from tests.conftest import make_request

    request = make_request(path=path)
    reduced = app._coerce_response(result)
    full = app._build_response(request, _Match(), result)
    assert type(reduced) is type(full)
    assert reduced.status_code == full.status_code
    assert reduced.body == full.body
    assert reduced.content_type == full.content_type


# ── end to end, the wire is unchanged ────────────────────────────────


def test_a_trivial_route_still_answers():
    with TestClient(_kitchen_sink()) as client:
        resp = client.get("/trivial")
    assert resp.status_code == 200
    assert resp.json() == {"a": 1}


def test_a_request_only_route_still_answers():
    with TestClient(_kitchen_sink()) as client:
        assert client.get("/request-only").json() == {"b": 2}


def test_the_excluded_shapes_still_answer_correctly():
    """The slow path must be unaffected by the fast path's reduction."""
    with TestClient(_kitchen_sink()) as client:
        assert client.get("/with-model").json() == {"name": "x"}
        assert client.get("/with-status").status_code == 201
        assert client.get("/with-injected").status_code == 202
        assert client.get("/with-params?q=7").json() == {"q": 7}
        assert client.get("/sync").json() == {"sync": True}


@pytest.mark.parametrize(
    ("returned", "expected_type", "expected_body"),
    [
        ({"a": 1}, "JSONResponse", b'{"a":1}'),
        ([1, 2], "JSONResponse", b"[1,2]"),
        ("text", "Response", b"text"),
        (b"raw", "Response", b"raw"),
    ],
)
def test_every_return_shape_coerces_the_same_on_the_fast_path(
    returned, expected_type, expected_body
):
    """The fast path bypasses the builder, so each return shape is checked."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return returned

    info = next(i for _m, p, i in _routes(app) if p == "/x")
    assert info.is_fast_eligible
    with TestClient(app) as client:
        resp = client.get("/x")
    assert resp.body == expected_body


def test_a_handler_returning_a_response_is_passed_through():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(body=b"mine", status_code=203)

    with TestClient(app) as client:
        resp = client.get("/x")
    assert resp.status_code == 203
    assert resp.body == b"mine"
