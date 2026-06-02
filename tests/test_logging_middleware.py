"""Tests for veloce.middleware.logging."""

from __future__ import annotations

import logging

import pytest

from veloce._constants import HEADER_X_REQUEST_ID
from veloce.http.request import Request
from veloce.middleware.logging import LoggingMiddleware, RequestIDMiddleware


def _request(headers=None) -> Request:
    return Request(
        method="GET",
        path="/",
        query_string="",
        headers=headers or {},
        body=b"",
    )


@pytest.fixture
def access_logger_state():
    """Snapshot and restore veloce.access logger state across the test."""
    logger = logging.getLogger("veloce.access")
    saved_level = logger.level
    saved_handlers = list(logger.handlers)
    saved_propagate = logger.propagate
    # Clear so each test starts from a known baseline.
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    try:
        yield logger
    finally:
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def test_default_bootstrap_sets_info_on_untouched_logger(access_logger_state):
    """With no pre-configuration, LoggingMiddleware sets INFO and adds a handler."""
    logger = access_logger_state
    assert logger.level == logging.NOTSET
    assert logger.handlers == []

    LoggingMiddleware()

    assert logger.level == logging.INFO
    assert len(logger.handlers) == 1


def test_preconfigured_level_is_respected(access_logger_state):
    """A pre-set WARNING level on veloce.access must survive middleware init."""
    logger = access_logger_state
    pre_handler = logging.NullHandler()
    logger.addHandler(pre_handler)
    logger.setLevel(logging.WARNING)

    LoggingMiddleware()

    assert logger.level == logging.WARNING
    # The pre-existing handler must still be the only handler — no
    # bootstrap StreamHandler appended.
    assert logger.handlers == [pre_handler]


def test_explicit_logger_argument_untouched(access_logger_state):
    """Passing logger= bypasses all bootstrap logic."""
    custom = logging.getLogger("test_explicit_logger_argument_untouched")
    custom.handlers = []
    custom.setLevel(logging.ERROR)

    mw = LoggingMiddleware(logger=custom)

    assert mw.logger is custom
    assert custom.level == logging.ERROR
    assert custom.handlers == []


def test_logging_respects_handler_only_or_level_only_config(access_logger_state):
    """Handlers and level are independent; bootstrapping must check each separately.

    R1 #23: the prior fix gated both `addHandler` and `setLevel(INFO)`
    on `not self.logger.handlers`, so a user who pre-installed a
    defensive `NullHandler` without setting a level would end up at
    NOTSET (inherits root → typically WARNING) and silently lose
    access logs.
    """
    # Case 1: user added a NullHandler but never set a level → we
    # MUST still bootstrap the level to INFO without adding a second
    # handler.
    logger = access_logger_state
    pre_handler = logging.NullHandler()
    logger.addHandler(pre_handler)
    assert logger.level == logging.NOTSET

    LoggingMiddleware()

    assert logger.level == logging.INFO  # level was bootstrapped
    assert logger.handlers == [pre_handler]  # no extra handler appended

    # Case 2: user set a level but installed no handler → we MUST
    # add our StreamHandler without overriding the level.
    logger.handlers = []
    logger.setLevel(logging.DEBUG)

    LoggingMiddleware()

    assert logger.level == logging.DEBUG  # pre-set level preserved
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)


# ── RequestIDMiddleware ─────────────────────────────────────────────


async def test_request_id_uses_incoming_header():
    mw = RequestIDMiddleware()
    request = _request({HEADER_X_REQUEST_ID: "abc-123"})
    await mw.process_request(request)
    assert request._state["request_id"] == "abc-123"


async def test_request_id_generated_when_header_missing():
    mw = RequestIDMiddleware()
    request = _request()
    await mw.process_request(request)
    assert request._state["request_id"]


async def test_request_id_generated_when_header_empty():
    # An empty incoming X-Request-ID is unusable; a fresh ID is generated
    # rather than propagating the empty string.
    mw = RequestIDMiddleware()
    request = _request({HEADER_X_REQUEST_ID: ""})
    await mw.process_request(request)
    assert request._state["request_id"]
