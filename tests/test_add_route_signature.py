"""`Veloce.add_route` keeps the documented signature it forwards to.

`Router.add_route` is the primary programmatic registration API and carries a
fully `Annotated[T, Doc(...)]`-typed signature. `Veloce` overrode it with
`def add_route(self, *args, **kwargs)` and no docstring, purely to bracket the
base call with a mutability check and a cache invalidation - so every user of
`help(app.add_route)`, and every editor signature hint, saw `(*args, **kwargs)`
and nothing else.

Restating the parameter list in the override would have added a ninth
hand-maintained copy of it - the duplication this same review reports elsewhere.
Instead `__signature__` and the docstring are re-pointed at the base, so
introspection recovers what the forward hides while the list stays in one place.
"""

from __future__ import annotations

import inspect

import pytest

from veloce import Veloce
from veloce.exceptions import SetupError
from veloce.routing.router import Router
from veloce.testclient import TestClient

# ── introspection sees the documented surface ────────────────────────


def test_the_override_does_not_report_star_args():
    """The defect: this was `(self, *args, **kwargs)`."""
    names = list(inspect.signature(Veloce.add_route).parameters)
    assert names != ["self", "args", "kwargs"]


def test_the_signature_matches_the_base():
    """One list, not two - that is the whole point of re-pointing rather than
    restating."""
    assert inspect.signature(Veloce.add_route) == inspect.signature(Router.add_route)


@pytest.mark.parametrize(
    "parameter", ["path", "handler", "methods", "dependencies", "response_model", "tags"]
)
def test_a_documented_parameter_is_visible(parameter):
    assert parameter in inspect.signature(Veloce.add_route).parameters


def test_the_override_is_documented():
    """It had no docstring at all."""
    doc = Veloce.add_route.__doc__ or ""
    assert doc.strip()
    assert "Router.add_route" in doc


# ── and it still does what the override exists for ───────────────────


def test_a_route_registered_through_it_serves():
    app = Veloce(openapi_url=None)

    async def handler():
        return {"ok": True}

    app.add_route("/x", handler, methods=["GET"])
    assert TestClient(app).get("/x").json() == {"ok": True}


def test_keyword_arguments_still_reach_the_base():
    """The forward must stay transparent - that is why it is `*args`."""
    app = Veloce(openapi_url=None)

    async def handler():
        return {"ok": True}

    app.add_route("/y", handler, methods=["POST"], name="named")
    assert "POST" in app.get_allowed_methods("/y")
    assert app.url_for("named") == "/y"


def test_registering_after_serving_is_refused():
    """The first reason the override exists: `_assert_mutable`.

    The lock is skipped under DEBUG/TESTING, so it is set directly here rather
    than by driving a request - the point is that the override calls the guard,
    not how the guard latches.
    """

    app = Veloce(openapi_url=None)

    async def handler():
        return {"ok": True}

    app._setup_locked = True
    with pytest.raises(SetupError):
        app.add_route("/late", handler, methods=["GET"])


def test_a_route_added_later_is_matched():
    """The second reason: the route caches must be dropped, or a route added
    after the first request would never resolve."""
    app = Veloce(openapi_url=None)

    async def first():
        return {"n": 1}

    async def second():
        return {"n": 2}

    app.add_route("/first", first, methods=["GET"])
    client = TestClient(app)
    assert client.get("/first").json() == {"n": 1}

    app.add_route("/second", second, methods=["GET"])
    assert TestClient(app).get("/second").json() == {"n": 2}
