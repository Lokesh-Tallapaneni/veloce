"""app.logger naming + app.trap_http_exception."""

from __future__ import annotations

import logging

from veloce import Veloce
from veloce.exceptions import (
    BadRequest,
    HTTPException,
    InternalServerError,
    NotFound,
)

# ── app.logger naming ───────────────────────────────────────────────


def test_logger_uses_import_name_when_provided():
    app = Veloce(import_name="myapp.api")
    assert isinstance(app.logger, logging.Logger)
    assert app.logger.name == "myapp.api"


def test_logger_uses_calling_module_when_import_name_omitted():
    """`import_name` defaults to the caller's `__name__`; logger follows it."""
    app = Veloce(title="My Cool API")
    # Auto-detected import_name = this test module.
    assert app.logger.name == __name__


def test_logger_is_singleton_per_name():
    """Two apps with the same import_name share the same Logger object."""
    a = Veloce(import_name="shared_logger_test")
    b = Veloce(import_name="shared_logger_test")
    assert a.logger is b.logger


# ── trap_http_exception ─────────────────────────────────────────────


def test_trap_returns_false_for_non_http_exception():
    app = Veloce()
    assert app.trap_http_exception(RuntimeError("x")) is False


def test_trap_returns_false_by_default():
    app = Veloce()
    assert app.trap_http_exception(NotFound()) is False


def test_trap_all_when_flag_set():
    app = Veloce()
    app.config["TRAP_HTTP_EXCEPTIONS"] = True
    assert app.trap_http_exception(NotFound()) is True
    assert app.trap_http_exception(InternalServerError()) is True


def test_trap_bad_request_errors_explicit():
    app = Veloce()
    app.config["TRAP_BAD_REQUEST_ERRORS"] = True
    assert app.trap_http_exception(BadRequest()) is True
    assert app.trap_http_exception(NotFound()) is True
    # 5xx not in scope.
    assert app.trap_http_exception(InternalServerError()) is False


def test_trap_bad_request_default_true_in_debug():
    """the built-in default: in debug mode 4xx are trapped unless explicitly disabled."""
    app = Veloce(debug=True)
    assert app.trap_http_exception(NotFound()) is True


def test_trap_bad_request_disabled_in_debug_via_explicit_false():
    app = Veloce(debug=True)
    app.config["TRAP_BAD_REQUEST_ERRORS"] = False
    assert app.trap_http_exception(NotFound()) is False


def test_trap_5xx_with_bad_request_flag_only():
    """5xx exceptions aren't trapped by TRAP_BAD_REQUEST_ERRORS alone."""
    app = Veloce()
    app.config["TRAP_BAD_REQUEST_ERRORS"] = True
    e = HTTPException(503, "Service Unavailable")
    assert app.trap_http_exception(e) is False
