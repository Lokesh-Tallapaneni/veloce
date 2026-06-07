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
