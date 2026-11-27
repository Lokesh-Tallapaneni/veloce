"""`Veloce.add_route` keeps the documented signature it forwards to.

`Router.add_route` is the primary programmatic registration API and carries a
fully `Annotated[T, Doc(...)]`-typed signature. The mutability check and cache
invalidation `Veloce` adds bracket `_add_route_options`, the one choke point
every registration path reaches, so `add_route` itself is inherited and
`help(app.add_route)` shows the documented parameters.
"""

from __future__ import annotations

import inspect

import pytest

from veloce import Veloce
from veloce.exceptions import SetupError
from veloce.routing.router import Router
from veloce.testclient import TestClient

# ── introspection sees the documented surface ────────────────────────


def test_the_application_inherits_the_documented_signature():
    """`Veloce` no longer overrides `add_route`: the bracketing lives on the one
    registration choke point, so the signature is the base's by construction."""
    assert "add_route" not in vars(Veloce)
    assert inspect.signature(Veloce.add_route) == inspect.signature(Router.add_route)


@pytest.mark.parametrize(
    "parameter", ["path", "handler", "methods", "dependencies", "response_model", "tags"]
)
def test_a_documented_parameter_is_visible(parameter):
    assert parameter in inspect.signature(Veloce.add_route).parameters


def test_the_options_after_methods_are_keyword_only():
    """A boolean such as `deprecated` cannot be passed positionally by mistake."""
    parameters = inspect.signature(Router.add_route).parameters
    positional = [
        name for name, p in parameters.items() if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]
    assert positional == ["self", "path", "handler", "methods"]


def test_the_documentation_is_the_bases():
    assert (Veloce.add_route.__doc__ or "").strip()


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
