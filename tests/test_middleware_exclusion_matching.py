"""`exclude_middleware` accepts a middleware class, not only its name string.

Exclusion was matched on `middleware_name` — the instance's `name` or its class
name — so three spellings failed, all of them in silence:

    exclude_middleware=["Base"]           + class Sub(Base)  -> Base still ran
    exclude_middleware=["BaseMiddleware"] + class Base       -> typo, Base still ran
    exclude_middleware=[Base]             + class Base       -> accepted, Base still ran

The third is the worst of the three. Passing the class is the obvious,
typo-proof spelling; it was accepted without complaint and did nothing. A route
that read as opting out of CSRF was running CSRF.

A class entry now matches by `isinstance`, so it covers subclasses and cannot be
misspelled — a wrong name is an import error at the point you write it. String
entries keep their exact-name meaning unchanged, because an instance-level
`name=` is how two instances of one class are told apart and matching those by
type would over-exclude.

The filtered chain is memoised per route and middleware generation, so the added
`isinstance` runs on the first request to a route that excludes something, and
never again.
"""

from __future__ import annotations

import pytest

from veloce import Middleware, Veloce
from veloce.testclient import TestClient


class Marking(Middleware):
    """Records that it ran, in a way a response assertion can see."""

    header = "X-Base"

    async def process_response(self, request, response):
        response.headers[type(self).header] = "ran"
        return response


class Derived(Marking):
    header = "X-Derived"


class Other(Middleware):
    async def process_response(self, request, response):
        response.headers["X-Other"] = "ran"
        return response


def _serve(middlewares, exclude):
    app = Veloce(openapi_url=None)
    for mw in middlewares:
        app.add_middleware(mw)

    @app.get("/", exclude_middleware=exclude)
    async def index():
        return {"ok": True}

    return TestClient(app).get("/")


# ── a class entry excludes ───────────────────────────────────────────


def test_a_class_entry_excludes_that_middleware():
    """The defect: this was accepted and did nothing."""
    assert "X-Base" not in _serve([Marking()], [Marking]).headers


def test_a_class_entry_excludes_a_subclass_instance():
    """The reason to match by type: a subclass is the same middleware."""
    assert "X-Derived" not in _serve([Derived()], [Marking]).headers


def test_a_subclass_entry_does_not_exclude_the_base():
    """Directional: excluding `Derived` must not silently disable `Marking`."""
    assert _serve([Marking()], [Derived]).headers["X-Base"] == "ran"


def test_a_class_entry_leaves_other_middleware_running():
    resp = _serve([Marking(), Other()], [Marking])
    assert "X-Base" not in resp.headers
    assert resp.headers["X-Other"] == "ran"


def test_class_and_string_entries_can_be_mixed():
    resp = _serve([Marking(), Other()], [Marking, "Other"])
    assert "X-Base" not in resp.headers
    assert "X-Other" not in resp.headers


def test_two_class_entries_both_apply():
    resp = _serve([Marking(), Other()], [Marking, Other])
    assert "X-Base" not in resp.headers
    assert "X-Other" not in resp.headers


# ── string entries are unchanged ─────────────────────────────────────
#
# The negatives. A string means the resolved `middleware_name`, exactly as
# before, because an instance-level `name=` is how two instances of one class are
# distinguished - matching those by type would over-exclude.


def test_a_string_entry_still_excludes_by_exact_name():
    assert "X-Base" not in _serve([Marking()], ["Marking"]).headers


def test_a_string_entry_still_does_not_reach_a_subclass():
    """Unchanged on purpose: `"Marking"` is not `Derived`'s name."""
    assert _serve([Derived()], ["Marking"]).headers["X-Derived"] == "ran"


def test_an_instance_name_still_distinguishes_two_of_one_class():
    first, second = Marking(), Other()
    first.name = "first"
    second.name = "second"
    resp = _serve([first, second], ["first"])
    assert "X-Base" not in resp.headers
    assert resp.headers["X-Other"] == "ran"


def test_a_class_entry_matching_an_instance_with_its_own_name_still_excludes():
    """A type match does not care what the instance called itself."""
    named = Marking()
    named.name = "custom"
    assert "X-Base" not in _serve([named], [Marking]).headers


def test_a_route_that_excludes_nothing_runs_everything():
    resp = _serve([Marking(), Other()], None)
    assert resp.headers["X-Base"] == "ran"
    assert resp.headers["X-Other"] == "ran"


def test_an_empty_exclusion_list_runs_everything():
    assert _serve([Marking()], []).headers["X-Base"] == "ran"


def test_an_unrelated_class_entry_excludes_nothing():
    assert _serve([Marking()], [Other]).headers["X-Base"] == "ran"


# ── the exclusion holds across the paths that rebuild a route ────────


def test_a_class_exclusion_survives_a_blueprint():
    from veloce import Blueprint

    bp = Blueprint("shop")

    @bp.get("/", exclude_middleware=[Marking])
    async def index():
        return {"ok": True}

    app = Veloce(openapi_url=None)
    app.add_middleware(Marking())
    app.register_blueprint(bp)
    assert "X-Base" not in TestClient(app).get("/").headers


def test_a_class_exclusion_survives_an_included_router():
    from veloce import Router

    router = Router()

    @router.get("/v", exclude_middleware=[Marking])
    async def index():
        return {"ok": True}

    app = Veloce(openapi_url=None)
    app.add_middleware(Marking())
    app.include_router(router, prefix="/api")
    assert "X-Base" not in TestClient(app).get("/api/v").headers


def test_the_exclusion_holds_on_a_second_request():
    """The filtered chain is memoised; the cached copy must match the first."""
    app = Veloce(openapi_url=None)
    app.add_middleware(Marking())

    @app.get("/", exclude_middleware=[Marking])
    async def index():
        return {"ok": True}

    client = TestClient(app)
    assert "X-Base" not in client.get("/").headers
    assert "X-Base" not in client.get("/").headers


def test_the_exclusion_is_recomputed_when_middleware_is_added_later():
    """Registering middleware after the route must not serve a stale chain."""
    app = Veloce(openapi_url=None)

    @app.get("/", exclude_middleware=[Marking])
    async def index():
        return {"ok": True}

    client = TestClient(app)
    client.get("/")
    app.add_middleware(Marking())
    app.add_middleware(Other())
    resp = client.get("/")
    assert "X-Base" not in resp.headers
    assert resp.headers["X-Other"] == "ran"


# ── a request-phase exclusion too, not just response ─────────────────


def test_a_class_exclusion_stops_a_request_phase_middleware():
    ran = []

    class Blocking(Middleware):
        async def process_request(self, request):
            ran.append(1)
            return None

    app = Veloce(openapi_url=None)
    app.add_middleware(Blocking())

    @app.get("/", exclude_middleware=[Blocking])
    async def index():
        return {"ok": True}

    TestClient(app).get("/")
    assert ran == []


def test_a_short_circuiting_middleware_is_excluded_by_class():
    """The case the finding names: a route opting out of an auth-adjacent
    middleware must actually opt out."""
    from veloce import Response

    class Gate(Middleware):
        async def process_request(self, request):
            return Response(body=b"denied", status_code=403)

    app = Veloce(openapi_url=None)
    app.add_middleware(Gate())

    @app.get("/open", exclude_middleware=[Gate])
    async def open_route():
        return {"ok": True}

    @app.get("/closed")
    async def closed_route():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/open").json() == {"ok": True}
    assert client.get("/closed").status_code == 403


# ── refusals ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("entry", [1, None, 3.5, object()])
def test_an_entry_that_is_neither_a_name_nor_a_class_is_refused(entry):
    """Silently ignoring one is what this finding is about."""
    app = Veloce(openapi_url=None)
    with pytest.raises((TypeError, ValueError)):

        @app.get("/", exclude_middleware=[entry])
        async def index():
            return {"ok": True}


def test_a_class_that_is_not_a_middleware_is_refused():
    app = Veloce(openapi_url=None)
    with pytest.raises((TypeError, ValueError)):

        @app.get("/", exclude_middleware=[dict])
        async def index():
            return {"ok": True}
