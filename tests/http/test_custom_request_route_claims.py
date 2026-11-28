"""What the "Customising the request or route" page says holds.

The page tells readers there is no `route_class` / `request_class` hook and maps
each thing they reach for one onto a supported mechanism. Each statement it
makes about behaviour is checked here, so the page cannot drift from the code.
"""

from __future__ import annotations

import inspect

import pytest

from tests.conftest import make_request
from veloce import Middleware, Request, Response, Router, Veloce


@pytest.mark.parametrize("name", ["route_class", "request_class", "router_class"])
def test_no_class_substitution_hook_exists(name):
    """`Veloce()` and `add_route` take no `route_class`/`request_class`/`router_class`."""
    assert name not in inspect.signature(Veloce.__init__).parameters
    assert name not in inspect.signature(Router.add_route).parameters


def test_request_is_slotted_and_rejects_arbitrary_attributes():
    """Per-request data goes on `request.state`, never on a `request` attribute."""
    request = make_request(path="/")
    assert not hasattr(request, "__dict__")
    with pytest.raises(AttributeError):
        request.not_a_framework_field = 1  # type: ignore[attr-defined]  # the claim under test
    request.state.anything = 1
    assert request.state.anything == 1


def test_middleware_wraps_the_handler_and_shares_state():
    """`process_request` runs before the handler, `process_response` after it."""

    class Timing(Middleware):
        async def process_request(self, request: Request) -> Response | None:
            request.state.started = True
            return None

        async def process_response(self, request: Request, response: Response) -> Response:
            response.headers["X-Handled"] = "1"
            return response

    app = Veloce(openapi_url=None)
    app.add_middleware(Timing)

    @app.get("/")
    async def index(request: Request):
        return {"started": request.state.started}

    response = app.test_client().get("/")
    assert response.json() == {"started": True}
    assert response.headers["X-Handled"] == "1"


def test_a_response_from_process_request_short_circuits_the_handler():
    class Refuse(Middleware):
        async def process_request(self, request: Request) -> Response | None:
            return Response(403, b"no")

    app = Veloce(openapi_url=None)
    app.add_middleware(Refuse)
    called = []

    @app.get("/")
    async def index(request: Request):
        called.append(True)
        return {}

    assert app.test_client().get("/").status_code == 403
    assert called == []
