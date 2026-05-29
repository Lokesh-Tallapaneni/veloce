"""OAuth2PasswordRequestFormStrict — grant_type is mandatory (SEC2)."""

from __future__ import annotations

import asyncio

from veloce import Depends, OAuth2PasswordRequestFormStrict, Veloce
from veloce.exceptions import HTTPException
from veloce.http.request import Request
from veloce.testclient import TestClient


def _make_form_request(body: bytes) -> Request:
    return Request(
        method="POST",
        path="/token",
        query_string="",
        headers=[(b"content-type", b"application/x-www-form-urlencoded")],
        body=body,
    )


def test_accepts_grant_type_password():
    app = Veloce(openapi_url=None)

    @app.post("/token")
    async def token(form: OAuth2PasswordRequestFormStrict = Depends()):
        return {"username": form.username, "grant_type": form.grant_type}

    with TestClient(app) as client:
        resp = client.post(
            "/token",
            data={"grant_type": "password", "username": "a", "password": "b"},
        )

    assert resp.json() == {"username": "a", "grant_type": "password"}


def test_missing_grant_type_is_422():
    app = Veloce(openapi_url=None)

    @app.post("/token")
    async def token(form: OAuth2PasswordRequestFormStrict = Depends()):
        return {}

    with TestClient(app) as client:
        resp = client.post("/token", data={"username": "a", "password": "b"})

    assert resp.status_code == 422


def test_wrong_grant_type_is_422():
    app = Veloce(openapi_url=None)

    @app.post("/token")
    async def token(form: OAuth2PasswordRequestFormStrict = Depends()):
        return {}

    with TestClient(app) as client:
        resp = client.post(
            "/token",
            data={"grant_type": "client_credentials", "username": "a", "password": "b"},
        )

    assert resp.status_code == 422


def test_strict_is_subclass_of_non_strict():
    from veloce import OAuth2PasswordRequestForm

    assert issubclass(OAuth2PasswordRequestFormStrict, OAuth2PasswordRequestForm)


def test_from_request_missing_grant_type_raises():
    """Direct from_request call must reject a missing grant_type field."""
    request = _make_form_request(b"username=a&password=b")
    try:
        asyncio.run(OAuth2PasswordRequestFormStrict.from_request(request))
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("expected HTTPException(422) for missing grant_type")


def test_from_request_wrong_grant_type_raises():
    """Direct from_request call must reject grant_type=bearer."""
    request = _make_form_request(b"grant_type=bearer&username=a&password=b")
    try:
        asyncio.run(OAuth2PasswordRequestFormStrict.from_request(request))
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("expected HTTPException(422) for grant_type=bearer")


def test_from_request_password_grant_type_succeeds():
    """Direct from_request call returns a populated form on grant_type=password."""
    request = _make_form_request(b"grant_type=password&username=a&password=b")
    form = asyncio.run(OAuth2PasswordRequestFormStrict.from_request(request))
    assert form.grant_type == "password"
    assert form.username == "a"
    assert form.password == "b"


def test_from_request_parses_form_only_once():
    """from_request must not invoke request.form() twice (no double parse)."""
    call_counter = {"n": 0}

    class CountingRequest(Request):
        async def form(self):  # type: ignore[override]
            call_counter["n"] += 1
            return await super().form()

    request = CountingRequest(
        method="POST",
        path="/token",
        query_string="",
        headers=[(b"content-type", b"application/x-www-form-urlencoded")],
        body=b"grant_type=password&username=a&password=b",
    )

    asyncio.run(OAuth2PasswordRequestFormStrict.from_request(request))
    assert call_counter["n"] == 1, (
        f"expected request.form() to be awaited once, got {call_counter['n']}"
    )
