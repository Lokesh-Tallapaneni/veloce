"""Path-parameter names must be legal Python identifiers.

A path parameter is bound to the handler as a keyword argument, so a name that
is not a valid identifier (or is a reserved keyword) can never bind. Veloce
rejects such names at registration with a clear, path-scoped `ValueError`
instead of letting them fail opaquely in the handler-plan binder at request
time. The check shares the placeholder scanner the matcher itself consumes, so
the validated names are exactly the names that would be bound.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.testclient import TestClient


def test_keyword_param_name_rejected():
    app = Veloce()

    with pytest.raises(ValueError, match="valid Python identifier"):

        @app.get("/{class}")
        async def handler(**kw):
            return {}


def test_non_identifier_param_name_rejected():
    app = Veloce()

    with pytest.raises(ValueError, match="valid Python identifier"):

        @app.get("/items/{2nd}")
        async def handler(**kw):
            return {}


def test_typed_param_with_keyword_name_rejected():
    # The name is validated even when a converter is attached (`{name:conv}`).
    app = Veloce()

    with pytest.raises(ValueError, match="valid Python identifier"):

        @app.get("/{from:int}")
        async def handler(**kw):
            return {}


def test_valid_param_name_registers_and_binds():
    app = Veloce()

    @app.get("/users/{user_id}")
    async def handler(user_id: str):
        return {"user_id": user_id}

    client = TestClient(app)
    assert client.get("/users/abc").json() == {"user_id": "abc"}
