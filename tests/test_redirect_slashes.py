"""redirect_slashes app option — trailing-slash redirect behaviour."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce


class TestRedirectSlashes:
    @pytest.mark.asyncio
    async def test_trailing_slash_redirect(self):
        app = Veloce(openapi_url=None, redirect_slashes=True)

        @app.get("/users/")
        async def users(request: Request):
            return [{"id": 1}]

        resp = await app.handle_request(make_request(path="/users"))
        assert resp.status_code == 307
        assert resp.headers.get("Location") == "/users/"

    @pytest.mark.asyncio
    async def test_no_redirect_when_disabled(self):
        app = Veloce(openapi_url=None, redirect_slashes=False)

        @app.get("/users/")
        async def users(request: Request):
            return [{"id": 1}]

        resp = await app.handle_request(make_request(path="/users"))
        assert resp.status_code == 404
