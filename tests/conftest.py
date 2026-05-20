"""Shared test fixtures for Veloce test suite."""

import pytest

from veloce import Request, Veloce


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
