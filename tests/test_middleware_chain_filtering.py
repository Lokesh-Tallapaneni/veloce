"""The compiled chains carry only middleware that implements the phase.

`Middleware` supplies a `process_request` and a `process_response` that return
straight away, so a middleware can implement one phase and ignore the other.
Compiled into the chain unconditionally, an ignored phase is an awaited
coroutine per request that does nothing - and most of the shipped middleware
implements one phase: compression and conditional-GET are response-only,
`ProxyFix` and the rate limiter are request-only.

Filtering is only safe because what is dropped is exactly the base no-op, so
these tests check both halves: the chain is shorter, and the behaviour through
it is unchanged.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce._pipeline import build_request_middleware, build_response_middleware
from veloce.http.response import Response
from veloce.middleware.base import Middleware
from veloce.middleware.compression import CompressionMiddleware
from veloce.middleware.proxy_fix import ProxyFix
from veloce.testclient import TestClient


class RequestOnly(Middleware):
    async def process_request(self, request):
        request.state.saw_request = True
        return None


class ResponseOnly(Middleware):
    async def process_response(self, request, response):
        response.headers["X-Response-Only"] = "1"
        return response


class BothPhases(Middleware):
    async def process_request(self, request):
        return None

    async def process_response(self, request, response):
        response.headers["X-Both"] = "1"
        return response


class NeitherPhase(Middleware):
    """A middleware that only audits, or only gates websockets."""


def _app(*middleware: Middleware) -> Veloce:
    app = Veloce(openapi_url=None)
    for mw in middleware:
        app.add_middleware(mw)

    @app.get("/")
    async def index():
        return {"ok": True}

    return app


def test_a_request_only_middleware_is_absent_from_the_response_chain() -> None:
    app = _app(RequestOnly())
    assert len(build_request_middleware(app)) == 1
    assert build_response_middleware(app) == ()


def test_a_response_only_middleware_is_absent_from_the_request_chain() -> None:
    app = _app(ResponseOnly())
    assert build_request_middleware(app) == ()
    assert len(build_response_middleware(app)) == 1


def test_a_middleware_implementing_neither_is_in_neither_chain() -> None:
    app = _app(NeitherPhase())
    assert build_request_middleware(app) == ()
    assert build_response_middleware(app) == ()


def test_a_middleware_implementing_both_is_in_both() -> None:
    app = _app(BothPhases())
    assert len(build_request_middleware(app)) == 1
    assert len(build_response_middleware(app)) == 1


@pytest.mark.parametrize(
    ("middleware", "phase", "expected"),
    [
        (CompressionMiddleware, "process_request", False),
        (CompressionMiddleware, "process_response", True),
        (ProxyFix, "process_request", True),
        (ProxyFix, "process_response", False),
    ],
)
def test_the_shipped_middleware_is_classified_as_documented(
    middleware: type, phase: str, expected: bool
) -> None:
    """The claim in the module docstring, checked against the actual classes."""
    from veloce._pipeline import _implements

    instance = middleware.__new__(middleware)
    assert _implements(instance, phase) is expected


def test_response_ordering_is_still_reversed_registration_order() -> None:
    """Filtering must not disturb the order of what remains."""
    order: list[str] = []

    class Recorder(Middleware):
        def __init__(self, tag: str) -> None:
            super().__init__()
            self.tag = tag

        async def process_response(self, request, response: Response) -> Response:
            order.append(self.tag)
            return response

    app = _app(Recorder("first"), RequestOnly(), Recorder("second"))
    with TestClient(app) as client:
        client.get("/")
    assert order == ["second", "first"], "reverse registration order, minus the filtered one"


def test_behaviour_through_a_mixed_stack_is_unchanged() -> None:
    """The end-to-end property: filtering drops no-ops, not effects."""
    app = _app(RequestOnly(), ResponseOnly(), BothPhases())
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["X-Response-Only"] == "1"
    assert response.headers["X-Both"] == "1"
