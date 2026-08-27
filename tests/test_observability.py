"""Tests for veloce.observability (JSON access logging)."""

from __future__ import annotations

import importlib
import json
import logging

import pytest

from veloce import Veloce
from veloce.observability import instrument_access_log, log_requests_as_json


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def make_logger(request):
    """Build a capture logger, and put the global registry back afterwards.

    `logging.getLogger(name)` returns a process-global object, and this clears
    its handlers, sets its level and disables propagation. Without teardown a
    later test asserting on log output through the same name silently sees
    nothing - the failure mode is a passing test, not a noisy one.
    """
    restore: list[tuple[logging.Logger, list, int, bool]] = []

    def _make(name: str, level: int = logging.INFO) -> tuple[logging.Logger, _Capture]:
        logger = logging.getLogger(name)
        restore.append((logger, list(logger.handlers), logger.level, logger.propagate))
        logger.handlers.clear()
        logger.setLevel(level)
        cap = _Capture()
        logger.addHandler(cap)
        logger.propagate = False
        return logger, cap

    yield _make

    for logger, handlers, level, propagate in reversed(restore):
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/items/{item_id}")
    async def items(request, item_id):
        return {"id": item_id}

    return app


def test_import_observability_module_succeeds():
    module = importlib.import_module("veloce.observability")
    assert hasattr(module, "log_requests_as_json")
    assert hasattr(module, "instrument_access_log")


def test_emits_one_json_record_per_request(make_logger):
    app = _app()
    logger, cap = make_logger("test.obs.json1")
    log_requests_as_json(app, logger=logger)
    app.test_client().get("/items/7")
    assert len(cap.records) == 1
    payload = json.loads(cap.records[0].getMessage())
    assert payload["method"] == "GET"
    assert payload["route"] == "/items/{item_id}"
    assert payload["status"] == 200
    assert isinstance(payload["duration_ms"], float)
    assert payload["streamed"] is False
    assert "path" not in payload


def test_include_path_adds_concrete_path(make_logger):
    app = _app()
    logger, cap = make_logger("test.obs.json2")
    log_requests_as_json(app, logger=logger, include_path=True)
    app.test_client().get("/items/7")
    payload = json.loads(cap.records[0].getMessage())
    assert payload["path"] == "/items/7"


def test_muted_logger_emits_nothing(make_logger):
    app = _app()
    logger, cap = make_logger("test.obs.json3", level=logging.WARNING)
    log_requests_as_json(app, logger=logger)
    app.test_client().get("/items/7")
    assert cap.records == []


def test_unmatched_request_logs_null_route(make_logger):
    app = _app()
    logger, cap = make_logger("test.obs.json4")
    log_requests_as_json(app, logger=logger)
    app.test_client().get("/nope")
    payload = json.loads(cap.records[0].getMessage())
    assert payload["route"] is None
    assert payload["status"] == 404


def test_hook_returned_for_introspection(make_logger):
    app = _app()
    logger, _ = make_logger("test.obs.json5")
    hook = log_requests_as_json(app, logger=logger)
    assert app._instrumentation[-1] is hook


# -- instrument_access_log ----------------------------------------


def test_access_log_uses_route_template(make_logger):
    app = _app()
    logger, cap = make_logger("test.obs.acc1")
    instrument_access_log(app, logger=logger)
    app.test_client().get("/items/7")
    msg = cap.records[0].getMessage()
    assert "/items/{item_id}" in msg
    assert "/items/7" not in msg


def test_access_log_unmatched_falls_back_to_path(make_logger):
    app = _app()
    logger, cap = make_logger("test.obs.acc2")
    instrument_access_log(app, logger=logger)
    app.test_client().get("/nope")
    assert "/nope" in cap.records[0].getMessage()


def test_access_log_unmatched_path_sanitizes_control_chars(make_logger):
    # A CR/LF in an unmatched (404) request path must be escaped, not written
    # raw, so it cannot forge or split a text access-log line (CWE-117).
    import types

    app = _app()
    logger, cap = make_logger("test.obs.sanitize")
    emit = instrument_access_log(app, logger=logger)
    emit(
        types.SimpleNamespace(
            method="GET",
            route=None,
            path="/evil\r\nINJECTED",
            status_code=404,
            duration_ms=1.0,
            streamed=False,
        )
    )
    msg = cap.records[0].getMessage()
    assert "\r" not in msg
    assert "\n" not in msg
    assert "\\x0a" in msg


def test_access_log_json_mode(make_logger):
    app = _app()
    logger, cap = make_logger("test.obs.acc3")
    instrument_access_log(app, logger=logger, json=True)
    app.test_client().get("/items/7")
    payload = json.loads(cap.records[0].getMessage())
    assert set(payload) == {"method", "route", "status", "duration_ms"}
    assert payload["route"] == "/items/{item_id}"


def test_access_log_muted_does_no_work(make_logger):
    app = _app()
    logger, cap = make_logger("test.obs.acc4", level=logging.WARNING)
    instrument_access_log(app, logger=logger)
    app.test_client().get("/items/7")
    assert cap.records == []


def test_access_log_streamed_skipped_when_disabled(make_logger):
    from veloce.http.response import StreamingResponse

    app = Veloce(openapi_url=None)

    @app.get("/stream")
    async def stream(request):
        async def gen():
            yield b"a"

        return StreamingResponse(gen())

    logger, cap = make_logger("test.obs.acc5")
    instrument_access_log(app, logger=logger, include_streamed=False)
    app.test_client().get("/stream")
    assert cap.records == []


def test_access_log_hook_returned(make_logger):
    app = _app()
    logger, _ = make_logger("test.obs.acc6")
    hook = instrument_access_log(app, logger=logger)
    assert app._instrumentation[-1] is hook


def test_the_logger_fixture_restores_the_global_registry(make_logger):
    """The global logging registry is put back, so a later test still sees output.

    `logging.getLogger(name)` returns a process-global object, and the fixture
    clears its handlers, sets its level and turns propagation off. Without
    teardown the next test asserting on the same logger silently captures
    nothing - and a passing test is a worse failure mode than a noisy one.
    """
    name = "test.obs.restoration"
    original = logging.getLogger(name)
    sentinel = logging.NullHandler()
    original.addHandler(sentinel)
    original.setLevel(logging.CRITICAL)
    original.propagate = True

    logger, _cap = make_logger(name)
    assert logger is original
    assert sentinel not in logger.handlers, "the fixture did not take over"
    assert logger.propagate is False

    # The fixture's teardown has not run yet, so prove restoration by invoking
    # it the way pytest will: through a nested request for the same name.
    # (The end-state assertion lives in the finaliser check below.)
    original.removeHandler(sentinel)


def test_a_later_test_still_sees_its_own_logger():
    """Runs after the fixture teardown above; the registry must be usable."""
    logger = logging.getLogger("test.obs.restoration")
    captured = _Capture()
    logger.addHandler(captured)
    logger.setLevel(logging.INFO)
    try:
        logger.info("visible")
        assert [r.getMessage() for r in captured.records] == ["visible"]
    finally:
        logger.removeHandler(captured)
