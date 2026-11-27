"""app.logger naming."""

from __future__ import annotations

import logging

from veloce import Veloce

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
