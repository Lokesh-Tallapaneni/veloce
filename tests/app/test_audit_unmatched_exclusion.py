"""An `exclude_middleware` name that matches nothing is reported.

`exclude_middleware=["CSRFMiddleware"]` on a webhook route is the documented way
to opt a route out of a middleware. The name is matched against
`Middleware.middleware_name`, and an unmatched one was simply skipped:

    registered middleware names: ['Tracer']
      /ok          X-Traced=<absent>     exclude_middleware=["Tracer"]
      /typo        X-Traced=1            exclude_middleware=["Tracerr"]
      /classname   X-Traced=1            exclude_middleware=["TracerMiddleware"]

No error at registration, no warning at startup, nothing at dispatch. The route
behaved exactly as if no exclusion had been written, while the source read as if
it were exempt - surfacing as a webhook that 403s with a CSRF middleware its
author believed they had opted out of.

Registration cannot answer this: routes are commonly registered before
middleware, and an exclusion may legitimately name a conditionally-installed one.
The audit asks it once the middleware set is final (`routes_final=True`), which
is where `veloce check` and the startup audit both ask.
"""

from __future__ import annotations

import pytest

from veloce import Middleware, Veloce
from veloce.audit import run
from veloce.testclient import TestClient

FINDING_ID = "exclude-middleware-unmatched"


class Tracer(Middleware):
    """Marks every response, so an exclusion that worked is visible."""

    async def process_response(self, request, response):
        response.headers["X-Traced"] = "1"
        return response


def _app(*exclusions: str | None) -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(Tracer())
    for index, exclusion in enumerate(exclusions):

        async def handler() -> dict:
            return {}

        handler.__name__ = f"handler_{index}"
        kwargs = {"exclude_middleware": [exclusion]} if exclusion else {}
        app.get(f"/r{index}", **kwargs)(handler)
    return app


def _ids(app: Veloce, *, routes_final: bool = True) -> list[str]:
    return [f.id for f in run(app, routes_final=routes_final)]


def _messages(app: Veloce) -> list[str]:
    return [f.message for f in run(app, routes_final=True) if f.id == FINDING_ID]


# ── an unmatched name is reported ────────────────────────────────────


@pytest.mark.parametrize("name", ["Tracerr", "TracerMiddleware", "tracer", "CSRFMiddleware"])
def test_an_unmatched_exclusion_is_reported(name):
    """The defect: every one of these was accepted in silence."""
    assert FINDING_ID in _ids(_app(name))


def test_the_message_names_the_route_and_the_name():
    message = _messages(_app("Tracerr"))[0]
    assert "Tracerr" in message
    assert "GET /r0" in message


def test_the_message_lists_the_registered_names():
    """What to write instead is the half a warning is useless without."""
    assert "Tracer" in _messages(_app("Tracerr"))[0]


def test_each_unmatched_name_is_reported_once():
    """Two routes with the same typo is one problem, not two."""
    app = _app("Tracerr", "Tracerr")
    assert len(_messages(app)) == 1


def test_two_different_typos_are_both_reported():
    assert len(_messages(_app("Tracerr", "Traceur"))) == 2


# ── a matching name, or none, is silent ──────────────────────────────


def test_a_matching_exclusion_is_not_reported():
    assert FINDING_ID not in _ids(_app("Tracer"))


def test_a_route_with_no_exclusion_is_not_reported():
    assert FINDING_ID not in _ids(_app(None))


def test_an_app_with_no_middleware_and_no_exclusion_is_not_reported():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x() -> dict:
        return {}

    assert FINDING_ID not in _ids(app)


def test_a_custom_name_is_matched():
    """A middleware registered under `name=` is excluded by that name."""
    app = Veloce(openapi_url=None)
    app.add_middleware(Tracer(), name="audit-trail")

    @app.get("/x", exclude_middleware=["audit-trail"])
    async def x() -> dict:
        return {}

    assert FINDING_ID not in _ids(app)


def test_the_class_name_of_a_renamed_middleware_is_reported():
    """The exact case the name override exists to make honest."""
    app = Veloce(openapi_url=None)
    app.add_middleware(Tracer(), name="audit-trail")

    @app.get("/x", exclude_middleware=["Tracer"])
    async def x() -> dict:
        return {}

    assert FINDING_ID in _ids(app)


# ── it is a startup question, not a registration one ─────────────────


def test_nothing_is_reported_before_the_routes_are_final():
    """Routes are commonly registered before middleware; asking early is wrong."""
    assert FINDING_ID not in _ids(_app("Tracerr"), routes_final=False)


def test_a_middleware_added_after_the_route_is_matched():
    """The reason the question waits: this order is ordinary and correct."""
    app = Veloce(openapi_url=None)

    @app.get("/x", exclude_middleware=["Tracer"])
    async def x() -> dict:
        return {}

    app.add_middleware(Tracer())
    assert FINDING_ID not in _ids(app)


def test_the_finding_can_be_silenced():
    app = _app("Tracerr")
    app.config["SILENCED_AUDIT_IDS"] = [FINDING_ID]
    assert FINDING_ID not in _ids(app)


# ── the behaviour itself is unchanged ────────────────────────────────


def test_a_matching_exclusion_still_skips_the_middleware():
    app = _app("Tracer")
    assert "X-Traced" not in TestClient(app).get("/r0").headers


def test_an_unmatched_exclusion_still_runs_the_middleware():
    """Reporting it must not change what dispatch does."""
    app = _app("Tracerr")
    assert TestClient(app).get("/r0").headers.get("X-Traced") == "1"


def test_a_route_without_an_exclusion_still_runs_the_middleware():
    app = _app(None)
    assert TestClient(app).get("/r0").headers.get("X-Traced") == "1"
