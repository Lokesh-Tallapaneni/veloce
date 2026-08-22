"""Shared test fixtures for Veloce test suite."""

import os

import pytest
from hypothesis import settings

from veloce import Request, Veloce

# Hypothesis profiles for the parser fuzz suite. The default keeps the
# per-example count modest so the fuzz tests run inside the normal `pytest`
# suite without slowing it down; the `ci` profile (selected by the CI fuzz leg
# via HYPOTHESIS_PROFILE=ci) explores more examples to catch parser
# regressions. A generous deadline avoids flaky timeouts under CPU contention.
settings.register_profile("default", deadline=None)
settings.register_profile("ci", max_examples=400, deadline=None)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture(autouse=True)
def _clear_graceful_drain_latch():
    """Clear the process-wide shutdown latch between tests.

    `HttpProtocol.start_graceful_drain()` sets a module global that makes every
    subsequently-admitted connection quiesce after one request. That is right in
    production, where shutdown is terminal, but a test that drives a server to
    shutdown would otherwise leave every later keep-alive and pipelining test
    serving a single request and failing. Clearing it is one global assignment.
    """
    yield
    from veloce.serving.protocol import HttpProtocol

    HttpProtocol.reset_graceful_drain()


@pytest.fixture
def app():
    """Fresh Veloce app with OpenAPI disabled for speed."""
    return Veloce(debug=True, openapi_url=None)


@pytest.fixture
def app_with_docs():
    """Veloce app with OpenAPI enabled."""
    return Veloce(debug=True)


def make_request(
    method: str = "GET",
    path: str = "/",
    headers: dict | None = None,
    body: bytes = b"",
    query_string: str = "",
) -> Request:
    """Factory for test Request objects."""
    return Request(
        method=method,
        path=path,
        query_string=query_string,
        headers=headers or {},
        body=body,
    )
