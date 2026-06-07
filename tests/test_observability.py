"""Tests for veloce.observability (JSON access logging)."""

from __future__ import annotations

import importlib
import json
import logging

from veloce import Veloce
from veloce.observability import instrument_access_log, log_requests_as_json


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _logger(name: str, level: int = logging.INFO) -> tuple[logging.Logger, _Capture]:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(level)
    cap = _Capture()
    logger.addHandler(cap)
    logger.propagate = False
    return logger, cap


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


def test_emits_one_json_record_per_request():
    app = _app()
    logger, cap = _logger("test.obs.json1")
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


def test_include_path_adds_concrete_path():
    app = _app()
    logger, cap = _logger("test.obs.json2")
    log_requests_as_json(app, logger=logger, include_path=True)
    app.test_client().get("/items/7")
    payload = json.loads(cap.records[0].getMessage())
    assert payload["path"] == "/items/7"


def test_muted_logger_emits_nothing():
    app = _app()
    logger, cap = _logger("test.obs.json3", level=logging.WARNING)
    log_requests_as_json(app, logger=logger)
    app.test_client().get("/items/7")
    assert cap.records == []


def test_unmatched_request_logs_null_route():
    app = _app()
    logger, cap = _logger("test.obs.json4")
    log_requests_as_json(app, logger=logger)
    app.test_client().get("/nope")
    payload = json.loads(cap.records[0].getMessage())
    assert payload["route"] is None
    assert payload["status"] == 404


def test_hook_returned_for_introspection():
    app = _app()
    logger, _ = _logger("test.obs.json5")
    hook = log_requests_as_json(app, logger=logger)
    assert app._instrumentation[-1] is hook


# -- instrument_access_log ----------------------------------------


def test_access_log_uses_route_template():
    app = _app()
    logger, cap = _logger("test.obs.acc1")
    instrument_access_log(app, logger=logger)
    app.test_client().get("/items/7")
    msg = cap.records[0].getMessage()
    assert "/items/{item_id}" in msg
    assert "/items/7" not in msg


def test_access_log_unmatched_falls_back_to_path():
    app = _app()
    logger, cap = _logger("test.obs.acc2")
    instrument_access_log(app, logger=logger)
    app.test_client().get("/nope")
    assert "/nope" in cap.records[0].getMessage()


def test_access_log_unmatched_path_sanitizes_control_chars():
    # A CR/LF in an unmatched (404) request path must be escaped, not written
    # raw, so it cannot forge or split a text access-log line (CWE-117).
    import types

    app = _app()
    logger, cap = _logger("test.obs.sanitize")
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


def test_access_log_json_mode():
    app = _app()
    logger, cap = _logger("test.obs.acc3")
    instrument_access_log(app, logger=logger, json=True)
    app.test_client().get("/items/7")
    payload = json.loads(cap.records[0].getMessage())
    assert set(payload) == {"method", "route", "status", "duration_ms"}
    assert payload["route"] == "/items/{item_id}"


def test_access_log_muted_does_no_work():
    app = _app()
    logger, cap = _logger("test.obs.acc4", level=logging.WARNING)
    instrument_access_log(app, logger=logger)
    app.test_client().get("/items/7")
    assert cap.records == []


def test_access_log_streamed_skipped_when_disabled():
    from veloce.http.response import StreamingResponse

    app = Veloce(openapi_url=None)

    @app.get("/stream")
    async def stream(request):
        async def gen():
            yield b"a"

        return StreamingResponse(gen())

    logger, cap = _logger("test.obs.acc5")
    instrument_access_log(app, logger=logger, include_streamed=False)
    app.test_client().get("/stream")
    assert cap.records == []


def test_access_log_hook_returned():
    app = _app()
    logger, _ = _logger("test.obs.acc6")
    hook = instrument_access_log(app, logger=logger)
    assert app._instrumentation[-1] is hook
