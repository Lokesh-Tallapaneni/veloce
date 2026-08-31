"""@app.teardown_request hooks — run after success, error, and 404.

`test_async_safety.TestTeardownAlwaysRuns` asserted the same three cases with
**async** hooks while these asserted them with sync ones, so between them the
two shapes were covered and neither module said so. The cases are parameterised
over both here, which is the claim that was actually being made twice.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce


def _register(app: Veloce, log: list, *, is_async: bool):
    """Register a teardown hook of the requested shape."""
    if is_async:

        @app.teardown_request
        async def on_teardown(exc):
            log.append(("teardown", exc))

    else:

        @app.teardown_request
        def on_teardown(exc):
            log.append(("teardown", exc))


HOOK_SHAPES = pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])


@HOOK_SHAPES
async def test_teardown_runs_after_success(is_async):
    app = Veloce(openapi_url=None)
    log: list = []
    _register(app, log, is_async=is_async)

    @app.get("/ok")
    async def ok(request: Request):
        return {"ok": True}

    await app.handle_request(make_request(path="/ok"))
    assert log == [("teardown", None)]


@HOOK_SHAPES
async def test_teardown_runs_after_error(is_async):
    app = Veloce(openapi_url=None)
    log: list = []
    _register(app, log, is_async=is_async)

    @app.get("/crash")
    async def crash(request: Request):
        raise ValueError("boom")

    await app.handle_request(make_request(path="/crash"))
    assert len(log) == 1
    assert type(log[0][1]).__name__ == "ValueError"


@HOOK_SHAPES
async def test_teardown_runs_after_404(is_async):
    app = Veloce(openapi_url=None)
    log: list = []
    _register(app, log, is_async=is_async)

    await app.handle_request(make_request(path="/nonexistent"))
    assert len(log) == 1


@HOOK_SHAPES
async def test_a_successful_request_passes_no_exception(is_async):
    """The negative for the error case: `exc` must be `None`, not truthy."""
    app = Veloce(openapi_url=None)
    log: list = []
    _register(app, log, is_async=is_async)

    @app.get("/ok")
    async def ok(request: Request):
        return {"ok": True}

    await app.handle_request(make_request(path="/ok"))
    assert log[0][1] is None
