"""An unhandled exception is recorded, not just turned into a 500.

The response for an unhandled failure is a generic 500 that says nothing, and
the exception does not leave the app - so an ASGI server's error logging never
sees it, and the native server has nothing to catch either. Without a log here
a production failure reaches nobody: no traceback, no path, no line.

The record goes through the app's own logger, so an operator turns it down the
way they turn down any other logger, and Python's handler of last resort puts it
on stderr with no logging configuration at all.

What must NOT be logged is as much of the contract as what must: a handled
exception is not a failure, and a propagated one already carries its traceback
to the caller.
"""

from __future__ import annotations

import logging

import pytest

from veloce import Veloce
from veloce.exceptions import HTTPException, NotFound
from veloce.testclient import TestClient


def _app(**config) -> Veloce:
    app = Veloce(openapi_url=None, import_name="logprobe")
    app.config.update(config)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom-marker")

    @app.get("/ok")
    async def ok() -> dict:
        return {"ok": True}

    @app.get("/missing")
    async def missing():
        raise NotFound()

    return app


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


# ── The failure is recorded ──────────────────────────────────────────


def test_an_unhandled_exception_is_logged(caplog):
    with caplog.at_level(logging.ERROR):
        response = TestClient(_app()).get("/boom")
    assert response.status_code == 500
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert isinstance(records[0].exc_info[1], RuntimeError)


def test_the_record_names_the_request_that_failed(caplog):
    """A traceback with no path is hard to place in a live log."""
    with caplog.at_level(logging.ERROR):
        TestClient(_app()).get("/boom")
    assert _records(caplog)[0].getMessage() == "Exception on /boom [GET]"


def test_the_record_carries_the_traceback(caplog):
    with caplog.at_level(logging.ERROR):
        TestClient(_app()).get("/boom")
    assert "kaboom-marker" in caplog.text
    assert "RuntimeError" in caplog.text


def test_the_method_is_the_one_that_was_called(caplog):
    app = _app()

    @app.post("/post-boom")
    async def post_boom():
        raise RuntimeError("kaboom-marker")

    with caplog.at_level(logging.ERROR):
        TestClient(app).post("/post-boom")
    assert _records(caplog)[0].getMessage() == "Exception on /post-boom [POST]"


def test_it_goes_through_the_apps_own_logger(caplog):
    """Which is what makes it configurable by ordinary means."""
    with caplog.at_level(logging.ERROR):
        TestClient(_app()).get("/boom")
    assert _records(caplog)[0].name == "logprobe"


def test_an_operator_can_silence_it(caplog):
    app = _app()
    logging.getLogger("logprobe").setLevel(logging.CRITICAL)
    try:
        with caplog.at_level(logging.ERROR):
            response = TestClient(app).get("/boom")
        assert response.status_code == 500
        assert _records(caplog) == []
    finally:
        logging.getLogger("logprobe").setLevel(logging.NOTSET)


def test_the_response_body_is_unchanged(caplog):
    """Logging must not leak the exception to the client."""
    with caplog.at_level(logging.ERROR):
        response = TestClient(_app()).get("/boom")
    assert response.json() == {"detail": "Internal Server Error"}
    assert "kaboom-marker" not in response.text


def test_each_failing_request_is_recorded_once(caplog):
    with caplog.at_level(logging.ERROR):
        client = TestClient(_app())
        for _ in range(3):
            client.get("/boom")
    assert len(_records(caplog)) == 3


# ── What must not be logged ──────────────────────────────────────────


def test_a_successful_request_logs_nothing(caplog):
    with caplog.at_level(logging.ERROR):
        TestClient(_app()).get("/ok")
    assert _records(caplog) == []


def test_a_handled_exception_is_not_logged(caplog):
    """A registered handler means the app dealt with it; it is not a failure."""
    app = _app()

    @app.exception_handler(RuntimeError)
    async def handle(request, exc):
        return {"handled": True}

    with caplog.at_level(logging.ERROR):
        response = TestClient(app).get("/boom")
    assert response.json() == {"handled": True}
    assert _records(caplog) == []


def test_an_http_exception_is_not_logged(caplog):
    """`abort(404)` is an outcome, not a crash."""
    with caplog.at_level(logging.ERROR):
        response = TestClient(_app()).get("/missing")
    assert response.status_code == 404
    assert _records(caplog) == []


def test_a_route_that_does_not_exist_is_not_logged(caplog):
    with caplog.at_level(logging.ERROR):
        response = TestClient(_app()).get("/nope")
    assert response.status_code == 404
    assert _records(caplog) == []


def test_a_propagated_exception_is_not_also_logged(caplog):
    """It re-raises to the caller with its traceback; logging would duplicate it."""
    app = _app(PROPAGATE_EXCEPTIONS=True)
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="kaboom-marker"):
        TestClient(app).get("/boom")
    assert _records(caplog) == []


def test_a_blueprint_scoped_handler_also_suppresses_the_log(caplog):
    from veloce import Blueprint

    app = Veloce(openapi_url=None, import_name="logprobe")
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.get("/boom")
    async def boom():
        raise RuntimeError("kaboom-marker")

    @bp.errorhandler(RuntimeError)
    async def handle(request, exc):
        return {"handled": True}

    app.register_blueprint(bp)
    with caplog.at_level(logging.ERROR):
        response = TestClient(app).get("/bp/boom")
    assert response.json() == {"handled": True}
    assert _records(caplog) == []


# ── Debug mode still records it ──────────────────────────────────────


def test_debug_mode_logs_it_too(caplog):
    """The traceback goes in the response body, but the operator still gets it."""
    with caplog.at_level(logging.ERROR):
        response = TestClient(_app(DEBUG=True)).get("/boom")
    assert response.status_code == 500
    assert len(_records(caplog)) == 1


# ── The out-of-band caller keeps working ─────────────────────────────


async def test_handle_user_exception_logs_without_a_request():
    """A background task or CLI hook has no request; the call must still work."""
    app = _app()
    response = await app.handle_user_exception(RuntimeError("kaboom-marker"))
    assert response.status_code == 500


async def test_handle_user_exception_names_the_request_when_given_one(caplog):
    from veloce import Request

    app = _app()
    request = Request(method="PUT", path="/thing", query_string="", headers={}, body=b"")
    with caplog.at_level(logging.ERROR):
        await app.handle_user_exception(RuntimeError("kaboom-marker"), request=request)
    assert _records(caplog)[0].getMessage() == "Exception on /thing [PUT]"


async def test_an_http_exception_out_of_band_is_still_not_logged(caplog):
    app = _app()
    with caplog.at_level(logging.ERROR):
        response = await app.handle_user_exception(HTTPException(404, "nope"))
    assert response.status_code == 404
    assert _records(caplog) == []
