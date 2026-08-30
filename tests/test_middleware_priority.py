"""Deterministic middleware ordering via `add_middleware(priority=...)`.

Higher priority runs earlier in the request phase and correspondingly later in
the response phase; equal priorities keep registration order. With no priority
set the chain is the plain registration order it has always been.
"""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Veloce
from veloce.http.response import Response
from veloce.middleware import Middleware


def _recorder(label: str, log: list[str]) -> type[Middleware]:
    class _MW(Middleware):
        name = label

        async def process_request(self, request):
            log.append(f"req:{label}")
            return None

        async def process_response(self, request, response):
            log.append(f"resp:{label}")
            return response

    return _MW


async def _drive(app: Veloce) -> None:
    """Run one request through real dispatch.

    Not `_run_request_middleware`: HTTP dispatch runs the compiled `cp.http_pre`
    tuple, and `build_request_middleware` filters that on `_implements`, so only
    this path can show that `priority=` reaches the chain the framework uses.
    """

    @app.get("/")
    async def _root(request):
        return Response(body=b"")

    await app.handle_request(make_request())


async def test_priority_orders_request_phase_high_first():
    log: list[str] = []
    app = Veloce()
    app.add_middleware(_recorder("low", log)(), priority=1)
    app.add_middleware(_recorder("high", log)(), priority=10)
    app.add_middleware(_recorder("mid", log)(), priority=5)

    await _drive(app)

    # Request phase: descending priority. Response phase: the reverse.
    assert log == [
        "req:high",
        "req:mid",
        "req:low",
        "resp:low",
        "resp:mid",
        "resp:high",
    ]


async def test_equal_priority_keeps_registration_order():
    log: list[str] = []
    app = Veloce()
    app.add_middleware(_recorder("a", log)(), priority=5)
    app.add_middleware(_recorder("b", log)(), priority=5)
    app.add_middleware(_recorder("c", log)(), priority=5)

    await _drive(app)

    assert log[:3] == ["req:a", "req:b", "req:c"]


async def test_no_priority_is_registration_order_unchanged():
    log: list[str] = []
    app = Veloce()
    app.add_middleware(_recorder("first", log)())
    app.add_middleware(_recorder("second", log)())

    # No priority ever set: the ordered rebuild is skipped and the chain is the
    # plain append-order list.
    assert app._any_priority is False
    assert [m.middleware_name for m in app.middlewares] == ["first", "second"]

    await _drive(app)
    assert log == ["req:first", "req:second", "resp:second", "resp:first"]


async def test_priority_interleaved_with_default_zero():
    log: list[str] = []
    app = Veloce()
    app.add_middleware(_recorder("default-a", log)())  # priority 0
    app.add_middleware(_recorder("boost", log)(), priority=100)
    app.add_middleware(_recorder("default-b", log)())  # priority 0

    await _drive(app)

    # The boosted middleware runs first in the request phase; the two
    # default-priority entries keep their relative registration order.
    assert log[:3] == ["req:boost", "req:default-a", "req:default-b"]


def test_priority_not_forwarded_to_middleware_constructor():
    """`priority` is popped before construction, so subclass __init__ is clean."""

    class StrictMW(Middleware):
        def __init__(self) -> None:
            super().__init__()

    app = Veloce()
    # Must not raise a TypeError about an unexpected `priority` kwarg.
    app.add_middleware(StrictMW, priority=3)
    assert app._any_priority is True
